from typing import Literal

from pydantic import BaseModel, Field


class MagnetRequest(BaseModel):
    magnet: str = Field(min_length=1, max_length=32_768)
    save_path: str | None = Field(default=None, max_length=4_096)
    select_first: bool = False


class FileSelection(BaseModel):
    selected: list[int]


class SpeedLimits(BaseModel):
    download_rate_limit: int = Field(default=0, ge=0, le=2_147_483_647)
    upload_rate_limit: int = Field(default=0, ge=0, le=2_147_483_647)


class TorrentRecord(BaseModel):
    save_path: str
    created_at: float
    magnet: str | None = None
    paused: bool = False
    select_first: bool = False
    selected: list[int] | None = None
    removal: Literal["keep", "delete"] | None = None


class TorrentStatus(BaseModel):
    info_hash: str
    name: str
    state: Literal[
        "metadata", "selecting", "checking", "downloading", "paused",
        "finished", "seeding", "removing", "error",
    ]
    progress: float
    download_rate: int
    upload_rate: int
    total_wanted: int
    total_done: int
    num_peers: int
    num_seeds: int
    eta: int | None
    save_path: str
    has_metadata: bool
    paused: bool
    select_first: bool
    error: str | None = None


class TorrentFile(BaseModel):
    index: int
    path: str
    size: int
    done: int
    selected: bool


class Snapshot(BaseModel):
    torrents: list[TorrentStatus]
    limits: SpeedLimits
    default_save_path: str
    warnings: list[str]
