"""audiocompress package."""

from .pipeline import JobConfig, JobResult, compress_batch, compress_one

__all__ = ["JobConfig", "JobResult", "compress_one", "compress_batch"]
__version__ = "0.2.0"
