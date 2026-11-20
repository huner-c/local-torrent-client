import asyncio
import logging
import math
import os
import re
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path

import libtorrent as lt

from .models import Snapshot, SpeedLimits, TorrentFile, TorrentRecord, TorrentStatus


log = logging.getLogger(__name__)
HASH = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
CHECKPOINT_INTERVAL = 30
ALERT_TIMEOUT = 60
NETWORK_SETTINGS = {
    "listen_interfaces": "0.0.0.0:6881,[::]:6881",
    "enable_dht": True,
    "enable_lsd": True,
    "enable_upnp": True,
    "enable_natpmp": True,
    "dht_bootstrap_nodes": (
        "router.bittorrent.com:6881,dht.transmissionbt.com:6881,router.utorrent.com:6881"
    ),
    "connections_limit": 500,
    "connection_speed": 40,
    "file_pool_size": 256,
    "max_queued_disk_bytes": 16 * 1024 * 1024,
    "max_metadata_size": 64 * 1024 * 1024,
    "alert_queue_size": 10_000,
    "ssrf_mitigation": True,
}


class TorrentError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass
class Job:
    key: str
    handle: object
    record: TorrentRecord
    name: str
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    checkpoint: asyncio.Future | None = None
    removal: asyncio.Future | None = None
    last_checkpoint: float = 0
    metadata_error: str | None = None
    refresh_metadata: bool = False
    deleting_payload: bool = False


def atomic_write(path: Path, data: bytes) -> None:
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def hash_keys(hashes) -> list[str]:
    keys = []
    if hashes.has_v1():
        keys.append(str(hashes.v1))
    if hashes.has_v2():
        keys.append(str(hashes.v2))
    return keys


def params_keys(params) -> list[str]:
    return hash_keys(params.ti.info_hashes() if params.ti is not None else params.info_hashes)


def magnet_params(magnet: str):
    if not magnet.lower().startswith("magnet:?"):
        raise TorrentError("Geçerli bir magnet bağlantısı girin.")
    try:
        return lt.parse_magnet_uri("magnet:" + magnet[7:])
    except (RuntimeError, ValueError, TypeError) as exc:
        raise TorrentError("Magnet bağlantısı geçersiz veya info hash eksik.") from exc


def priorities(info, record: TorrentRecord) -> list[int]:
    files = info.files()
    selected = None if record.selected is None else set(record.selected)
    return [
        4 if not record.select_first and not files.file_flags(index) & lt.file_storage.flag_pad_file
        and (selected is None or index in selected) else 0
        for index in range(files.num_files())
    ]


class TorrentManager:
    def __init__(self, root: Path, network_settings: dict | None = None):
        self.root = root.resolve()
        self.state_dir = self.root / "data" / "state"
        self.default_save_path = self.root / "downloads"
        self.jobs: dict[str, Job] = {}
        self.restore_warnings: list[str] = []
        self.network_settings = network_settings or {}
        self.limits = SpeedLimits()
        self.session = None
        self.monitor: asyncio.Task | None = None
        self.state_lock = None
        self.cached_snapshot: Snapshot | None = None
        self.sampled_at = 0.0

    def _state_file(self, key: str, extension: str) -> Path:
        return self.state_dir / f"{key}.{extension}"

    def _lock_state_dir(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        stream = (self.state_dir / ".lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            stream.close()
            raise RuntimeError("Bu veri klasörünü başka bir Local Torrent işlemi kullanıyor.") from exc
        self.state_lock = stream

    def start(self) -> None:
        self._lock_state_dir()
        limits_file = self.state_dir.parent / "settings.json"
        if limits_file.exists():
            try:
                self.limits = SpeedLimits.model_validate_json(limits_file.read_bytes())
            except (OSError, ValueError):
                log.exception("Hız limitleri yüklenemedi")
                self.restore_warnings.append("Kayıtlı hız limitleri okunamadı.")
        category = lt.alert.category_t
        self.session = lt.session({
            **NETWORK_SETTINGS,
            **self.network_settings,
            **self.limits.model_dump(),
            "alert_mask": int(category.error_notification | category.storage_notification
                              | category.status_notification),
        })
        # Put local peers in the global bandwidth class as well.
        peer_classes = lt.ip_filter()
        global_class = 1 << lt.session.global_peer_class_id
        peer_classes.add_rule("0.0.0.0", "255.255.255.255", global_class)
        peer_classes.add_rule("::", "ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff", global_class)
        self.session.set_peer_class_filter(peer_classes)
        for path in self.state_dir.glob("*.json"):
            if HASH.fullmatch(path.stem):
                self._restore(path)
        self.monitor = asyncio.create_task(self._monitor())

    def _restore(self, path: Path) -> None:
        key = path.stem
        try:
            record = TorrentRecord.model_validate_json(path.read_bytes())
            if record.removal == "keep":
                self._purge_state(key)
                return

            params = None
            resume = self._state_file(key, "fastresume")
            if resume.exists():
                try:
                    candidate = lt.read_resume_data(resume.read_bytes())
                    if key not in params_keys(candidate):
                        raise ValueError("Resume hash uyuşmuyor")
                    params = candidate
                except (OSError, RuntimeError, ValueError):
                    log.warning("Resume dosyası geçersiz: %s", resume, exc_info=True)
                    self.restore_warnings.append(f"{key[:12]} için resume verisi okunamadı. Disk kontrolü gerekebilir.")

            metadata = self._state_file(key, "torrent")
            if (params is None or params.ti is None) and metadata.exists():
                try:
                    loaded = lt.load_torrent_buffer(metadata.read_bytes())
                    if key not in params_keys(loaded):
                        raise ValueError("Torrent hash uyuşmuyor")
                    if params is None:
                        params = loaded
                    else:
                        params.ti = loaded.ti
                except (OSError, RuntimeError, ValueError):
                    if record.magnet is None:
                        raise
                    log.warning("Magnet metadata önbelleği okunamadı: %s", metadata, exc_info=True)
            if params is None and record.magnet is not None:
                params = magnet_params(record.magnet)
            if params is None or key not in params_keys(params):
                raise ValueError("Torrent metadata bulunamadı")

            self._check_storage(key, record, params.ti)
            job = self._register(key, params, record)
            if record.removal == "delete":
                self._send_removal(job)
        except (OSError, RuntimeError, ValueError, TorrentError):
            log.exception("Torrent yüklenemedi: %s", key)
            self.restore_warnings.append(f"{key[:12]} kaydı yüklenemedi. Durum dosyaları korundu.")

    def _download_path(self, value: str | None, key: str) -> Path:
        value = value.strip() if value else ""
        base = Path(value).expanduser() if value else self.default_save_path
        if (base.drive and not base.is_absolute()) or base.anchor.startswith("\\\\"):
            raise TorrentError("Yerel diskte tam bir klasör yolu girin.")
        return (self.root / base).resolve() / key

    def _check_storage(self, key: str, record: TorrentRecord, info) -> None:
        directory = Path(record.save_path)
        if directory.name != key or not directory.is_absolute() or directory.resolve() != directory:
            raise TorrentError("Torrent klasörü güvenli bir yerel yol olmalı.")
        directory.mkdir(parents=True, exist_ok=True)
        if info is None:
            return
        existing = next(directory.iterdir(), None) is not None
        files = info.files()
        for index in range(files.num_files()):
            relative = Path(files.file_path(index))
            if relative.anchor or ".." in relative.parts or files.file_flags(index) & lt.file_storage.flag_symlink:
                raise TorrentError("Torrent metadata geçersiz bir dosya yolu içeriyor.")
            if existing:
                self._check_existing_path(directory, relative)

    @staticmethod
    def _check_existing_path(directory: Path, relative: Path) -> None:
        current = directory
        for part in relative.parts:
            current = current / part
            try:
                attributes = current.lstat()
            except FileNotFoundError:
                return
            windows_attributes = getattr(attributes, "st_file_attributes", 0)
            if stat.S_ISLNK(attributes.st_mode) or windows_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise TorrentError("Torrent yolu sembolik bağlantı veya yönlendirme içeriyor.")

    def _register(self, key: str, params, record: TorrentRecord) -> Job:
        flags = lt.torrent_flags
        params.save_path = record.save_path
        params.storage_mode = lt.storage_mode_t.storage_mode_sparse
        params.renamed_files = {}
        params.flags &= ~(flags.auto_managed | flags.paused | flags.upload_mode | flags.seed_mode)
        params.flags |= flags.duplicate_is_error
        if record.paused or record.removal:
            params.flags |= flags.paused
        if params.ti is None:
            # Keep exchanging metadata with peers while payload requests are disabled.
            params.flags |= flags.default_dont_download | flags.upload_mode
        else:
            params.flags &= ~flags.default_dont_download
            params.file_priorities = priorities(params.ti, record)
            params.piece_priorities = []
        handle = self.session.add_torrent(params)
        name = params.ti.name() if params.ti is not None else params.name
        job = Job(key=key, handle=handle, record=record, name=name or key)
        self.jobs[key] = job
        self.cached_snapshot = None
        return job

    def add_magnet(self, magnet: str, save_path: str | None, select_first: bool) -> str:
        magnet = magnet.strip()
        return self._add(magnet_params(magnet), save_path, select_first, magnet=magnet)

    def add_torrent(self, data: bytes, save_path: str | None, select_first: bool) -> str:
        try:
            params = lt.load_torrent_buffer(data)
        except (RuntimeError, ValueError, TypeError) as exc:
            raise TorrentError(".torrent dosyası geçersiz veya eksik.") from exc
        return self._add(params, save_path, select_first, metadata=data)

    def _add(self, params, save_path, select_first, *, magnet=None, metadata=None) -> str:
        keys = params_keys(params)
        if not keys:
            raise TorrentError("Torrent info hash bulunamadı.")
        if any(self._state_file(key, "json").exists() for key in keys):
            raise TorrentError("Bu torrent zaten listede veya korunmuş bir kaydı var.", 409)
        key = keys[0]
        try:
            record = TorrentRecord(save_path=str(self._download_path(save_path, key)),
                                   created_at=time.time(), magnet=magnet, select_first=select_first)
            self._check_storage(key, record, params.ti)
            if metadata is not None:
                atomic_write(self._state_file(key, "torrent"), metadata)
            try:
                self._write_record(key, record)
                job = self._register(key, params, record)
            except (OSError, ValueError, RuntimeError, TorrentError):
                self._purge_state(key)
                raise
        except (OSError, ValueError) as exc:
            raise TorrentError(f"İndirme klasörü veya durum dosyası açılamadı: {exc}") from exc
        except RuntimeError as exc:
            if "duplicate" in str(exc).lower():
                raise TorrentError("Bu torrent zaten listede.", 409) from exc
            raise TorrentError(f"Torrent eklenemedi: {exc}") from exc
        self._request_checkpoint(job)
        return key

    def _write_record(self, key: str, record: TorrentRecord) -> None:
        atomic_write(self._state_file(key, "json"), record.model_dump_json().encode("utf-8"))

    def _change_record(self, job: Job, **changes) -> None:
        record = job.record.model_copy(update=changes)
        try:
            self._write_record(job.key, record)
        except OSError as exc:
            raise TorrentError(f"Torrent durumu diske yazılamadı: {exc}", 500) from exc
        job.record = record
        self.cached_snapshot = None

    def _get(self, key: str) -> Job:
        job = self.jobs.get(key)
        if job is None:
            raise TorrentError("Torrent bulunamadı.", 404)
        if job.record.removal:
            raise TorrentError("Torrent kaldırılıyor.", 409)
        return job

    def list_files(self, key: str) -> list[TorrentFile]:
        job = self._get(key)
        info = job.handle.torrent_file()
        if info is None:
            raise TorrentError("Torrent metadata henüz gelmedi.", 409)
        files = info.files()
        progress = job.handle.file_progress(lt.torrent_handle.piece_granularity)
        selected = None if job.record.selected is None else set(job.record.selected)
        return [
            TorrentFile(index=index, path=files.file_path(index).replace(os.sep, "/"),
                        size=files.file_size(index), done=progress[index],
                        selected=selected is None or index in selected)
            for index in range(files.num_files()) if not files.file_flags(index) & lt.file_storage.flag_pad_file
        ]

    async def select_files(self, key: str, selected: list[int]) -> None:
        job = self._get(key)
        async with job.lock:
            self._get(key)
            info = job.handle.torrent_file()
            if info is None or job.metadata_error:
                raise TorrentError(job.metadata_error or "Torrent metadata henüz gelmedi.", 409)
            files = info.files()
            indices = sorted(set(selected))
            if any(index < 0 or index >= files.num_files() or files.file_flags(index) & lt.file_storage.flag_pad_file
                   for index in indices):
                raise TorrentError("Seçimde geçersiz bir dosya bulunuyor.")
            self._change_record(job, selected=indices, select_first=False)
            job.handle.prioritize_files(priorities(info, job.record))
            await self._checkpoint_now(job)

    async def set_paused(self, key: str, paused: bool) -> None:
        job = self._get(key)
        async with job.lock:
            self._get(key)
            if job.metadata_error and not paused:
                raise TorrentError(job.metadata_error, 409)
            self._change_record(job, paused=paused)
            if paused:
                job.handle.pause()
            else:
                job.handle.clear_error()
                job.handle.resume()
            await self._checkpoint_now(job)

    def set_limits(self, limits: SpeedLimits) -> SpeedLimits:
        try:
            atomic_write(self.state_dir.parent / "settings.json", limits.model_dump_json().encode("utf-8"))
        except OSError as exc:
            raise TorrentError(f"Hız limitleri kaydedilemedi: {exc}", 500) from exc
        self.session.apply_settings(limits.model_dump())
        self.limits = limits
        self.cached_snapshot = None
        return limits

    def _request_checkpoint(self, job: Job) -> asyncio.Future:
        if job.checkpoint is not None:
            return job.checkpoint
        pending = asyncio.get_running_loop().create_future()
        job.checkpoint = pending
        job.last_checkpoint = time.monotonic()
        job.handle.save_resume_data(lt.torrent_handle.flush_disk_cache | lt.torrent_handle.save_info_dict)
        return pending

    async def _checkpoint_now(self, job: Job) -> None:
        try:
            if job.checkpoint is not None:
                await asyncio.wait_for(asyncio.shield(job.checkpoint), ALERT_TIMEOUT)
            error = await asyncio.wait_for(asyncio.shield(self._request_checkpoint(job)), ALERT_TIMEOUT)
        except asyncio.TimeoutError as exc:
            raise TorrentError("Resume verisi diske yazılırken zaman aşımı oluştu.", 504) from exc
        if error is not None:
            raise TorrentError(f"Resume verisi kaydedilemedi: {error}", 500)

    def _save_checkpoint(self, job: Job, alert) -> None:
        error = None
        try:
            if isinstance(alert, lt.save_resume_data_failed_alert):
                error = alert.message()
            else:
                params = alert.params
                atomic_write(self._state_file(job.key, "fastresume"), bytes(lt.write_resume_data_buf(params)))
                metadata = self._state_file(job.key, "torrent")
                if params.ti is not None and (job.refresh_metadata or not metadata.exists()):
                    metainfo = lt.write_torrent_file(params, lt.write_flags.allow_missing_piece_layer)
                    atomic_write(metadata, lt.bencode(metainfo))
                    job.refresh_metadata = False
                elif job.refresh_metadata:
                    job.last_checkpoint = 0
        except (OSError, RuntimeError, ValueError, TypeError) as exc:
            error = str(exc)
        if error:
            log.warning("%s resume kaydı: %s", job.key, error)
        if job.checkpoint is not None:
            job.checkpoint.set_result(error)
            job.checkpoint = None

    def _metadata_received(self, job: Job) -> None:
        try:
            info = job.handle.torrent_file()
            self._check_storage(job.key, job.record, info)
            job.name = info.name()
            job.handle.prioritize_files(priorities(info, job.record))
            job.handle.unset_flags(lt.torrent_flags.default_dont_download | lt.torrent_flags.upload_mode)
            job.refresh_metadata = True
            self._request_checkpoint(job)
        except (OSError, RuntimeError, TorrentError) as exc:
            job.metadata_error = str(exc)
            job.handle.pause()

    async def remove(self, key: str, delete_files: bool) -> None:
        job = self._get(key)
        async with job.lock:
            self._get(key)
            if delete_files:
                try:
                    self._check_storage(key, job.record, job.handle.torrent_file())
                except OSError as exc:
                    raise TorrentError(f"Torrent klasörüne erişilemiyor: {exc}", 409) from exc
            self._change_record(job, removal="delete" if delete_files else "keep")
            pending = self._send_removal(job)
            try:
                error = await asyncio.wait_for(asyncio.shield(pending), ALERT_TIMEOUT)
            except asyncio.TimeoutError as exc:
                raise TorrentError("Kaldırma sürüyor. Durumu listeden takip edebilirsiniz.", 504) from exc
            if error is not None:
                raise TorrentError(error, 500)

    def _send_removal(self, job: Job) -> asyncio.Future:
        job.removal = asyncio.get_running_loop().create_future()
        job.deleting_payload = job.record.removal == "delete" and job.handle.torrent_file() is not None
        if job.deleting_payload:
            self.session.remove_torrent(job.handle, lt.options_t.delete_files)
        else:
            # There are no payload files to delete before metadata arrives.
            self.session.remove_torrent(job.handle)
        return job.removal

    def _purge_state(self, key: str) -> None:
        for extension in ("fastresume", "torrent", "json"):
            self._state_file(key, extension).unlink(missing_ok=True)

    def _finish_removal(self, job: Job, error: str | None) -> None:
        if error is None:
            try:
                self._purge_state(job.key)
            except OSError as exc:
                error = f"Torrent durumu silinemedi: {exc}"
        if error:
            log.warning("%s kaldırma: %s", job.key, error)
            self.restore_warnings.append(error)
        if job.removal is not None:
            job.removal.set_result(error)
        if job.checkpoint is not None:
            job.checkpoint.set_result("Torrent kaldırıldı.")
        self.jobs.pop(job.key, None)
        self.cached_snapshot = None

    def _process_alerts(self) -> None:
        for alert in self.session.pop_alerts():
            if not isinstance(alert, (lt.save_resume_data_alert, lt.save_resume_data_failed_alert,
                                      lt.metadata_received_alert, lt.torrent_finished_alert,
                                      lt.torrent_removed_alert, lt.torrent_deleted_alert,
                                      lt.torrent_delete_failed_alert)):
                continue
            job = next((item for item in self.jobs.values() if item.handle == alert.handle), None)
            if job is None and hasattr(alert, "info_hashes"):
                hashes = alert.info_hashes
                if callable(hashes):
                    hashes = hashes()
                job = next((self.jobs[key] for key in hash_keys(hashes) if key in self.jobs), None)
            if job is None:
                continue
            if isinstance(alert, (lt.save_resume_data_alert, lt.save_resume_data_failed_alert)):
                if not job.record.removal:
                    self._save_checkpoint(job, alert)
            elif isinstance(alert, lt.torrent_removed_alert):
                if job.record.removal and not job.deleting_payload:
                    self._finish_removal(job, None)
            elif isinstance(alert, lt.torrent_deleted_alert):
                self._finish_removal(job, None)
            elif isinstance(alert, lt.torrent_delete_failed_alert):
                self._finish_removal(job, f"Torrent dosyaları silinemedi: {alert.message()}")
            elif not job.record.removal:
                self.cached_snapshot = None
                if isinstance(alert, lt.metadata_received_alert):
                    self._metadata_received(job)
                elif not job.record.select_first and job.record.selected != []:
                    job.refresh_metadata = job.record.magnet is not None
                    self._request_checkpoint(job)

    async def _monitor(self) -> None:
        while True:
            self._process_alerts()
            now = time.monotonic()
            for job in self.jobs.values():
                if not job.record.removal and not job.record.paused and now - job.last_checkpoint >= CHECKPOINT_INTERVAL:
                    self._request_checkpoint(job)
            await asyncio.sleep(0.2)

    def snapshot(self) -> Snapshot:
        now = time.monotonic()
        if self.cached_snapshot is None or now - self.sampled_at >= 0.95:
            statuses = [self._status(job) for job in sorted(
                self.jobs.values(), key=lambda item: item.record.created_at, reverse=True)]
            self.cached_snapshot = Snapshot(torrents=statuses, limits=self.limits,
                                            default_save_path=str(self.default_save_path),
                                            warnings=self.restore_warnings.copy())
            self.sampled_at = now
        return self.cached_snapshot

    def _status(self, job: Job) -> TorrentStatus:
        status = job.handle.status(0)
        total_wanted = max(0, status.total_wanted)
        total_done = max(0, status.total_wanted_done)
        error = job.metadata_error or (status.errc.message() if status.errc.value() else None)
        phases = lt.torrent_status.states
        if job.record.removal:
            state = "removing"
        elif error:
            state = "error"
        elif job.record.paused:
            state = "paused"
        elif not status.has_metadata:
            state = "metadata"
        elif job.record.select_first or job.record.selected == []:
            state = "selecting"
        elif status.state in (phases.checking_files, phases.checking_resume_data):
            state = "checking"
        elif status.is_seeding:
            state = "seeding"
        elif status.is_finished:
            state = "finished"
        else:
            state = "downloading"
        progress = 100.0 * min(1, total_done / total_wanted) if total_wanted else 0.0
        if state in ("seeding", "finished"):
            progress = 100.0
        eta = math.ceil((total_wanted - total_done) / status.download_payload_rate) if (
            state == "downloading" and status.download_payload_rate > 0 and total_wanted > total_done
        ) else None
        return TorrentStatus(info_hash=job.key, name=job.name,
                             state=state, progress=progress, download_rate=status.download_rate,
                             upload_rate=status.upload_rate, total_wanted=total_wanted,
                             total_done=total_done, num_peers=status.num_peers,
                             num_seeds=status.num_seeds, eta=eta, save_path=job.record.save_path,
                             has_metadata=status.has_metadata, paused=job.record.paused,
                             select_first=job.record.select_first, error=error)

    async def close(self) -> None:
        try:
            if self.monitor is not None:
                self.session.pause()
                results = await asyncio.gather(
                    *(self._checkpoint_now(job) for job in list(self.jobs.values()) if not job.record.removal),
                    *(asyncio.wait_for(asyncio.shield(job.removal), ALERT_TIMEOUT)
                      for job in list(self.jobs.values()) if job.removal is not None),
                    return_exceptions=True,
                )
                for result in results:
                    if isinstance(result, Exception):
                        log.error("Kapanışta torrent durumu: %s", result)
        finally:
            if self.monitor is not None:
                self.monitor.cancel()
                await asyncio.gather(self.monitor, return_exceptions=True)
            self.jobs.clear()
            if self.session is not None:
                await asyncio.to_thread(self._close_session)
            if self.state_lock is not None:
                self.state_lock.close()

    def _close_session(self) -> None:
        self.session = None
