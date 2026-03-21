"""Command and result dataclasses for the kasa_manager IPC protocol.

Parent to worker:  PlugCommand (serialised as plain dict via multiprocessing.Queue)
Worker to parent:  PlugResult  (serialised as plain dict via multiprocessing.Queue)
"""

from __future__ import annotations

from dataclasses import dataclass, asdict, field
from typing import Optional
import uuid


@dataclass
class PlugCommand:
    """A command sent from the parent process to the kasa worker subprocess."""
    controller_id: int          # 0-2, or -1 for startup-query-only commands
    role: str                   # 'heating' | 'cooling' | ''
    url: str                    # IP address or hostname of the Kasa plug
    action: str                 # 'on' | 'off' | 'query'
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        known = {'controller_id', 'role', 'url', 'action', 'request_id'}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class PlugResult:
    """A result returned by the kasa worker subprocess to the parent process."""
    request_id: str
    controller_id: int
    role: str
    url: str
    action: str                     # 'on' | 'off' | 'query'
    success: bool
    error: Optional[str]
    elapsed_ms: int
    state: Optional[str] = None    # 'on' | 'off' | None  (populated for 'query' action)

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        known = {
            'request_id', 'controller_id', 'role', 'url', 'action',
            'success', 'error', 'elapsed_ms', 'state'
        }
        return cls(**{k: v for k, v in d.items() if k in known})
