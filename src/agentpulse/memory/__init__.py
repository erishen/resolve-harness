"""Memory subpackage.

Two kinds of memory, both pluggable behind small interfaces:

- ShortTermMemory — the live conversation transcript (list of messages).
  Owned by the harness, threaded through every loop iteration, trimmed when
  it grows too long.
- LongTermMemory — durable facts (key/value + scope), stored in SQLite so
  they survive restarts and can be shared across sessions. The agent reads
  and writes it through tools (`remember` / `recall`).
"""

from .long_term import LongTermMemory
from .short_term import ShortTermMemory

__all__ = ["ShortTermMemory", "LongTermMemory"]
