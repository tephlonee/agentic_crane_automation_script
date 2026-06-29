"""
core/message_bus.py — the central post office for all inter-agent messages.

DESIGN: Singleton
  There is exactly ONE MessageBus in the entire program, shared by every agent.
  This means agents never need a reference to each other — they only need the bus.
  Decoupling agents this way is a core principle of MAS design.

HOW IT WORKS:
  1. When an agent starts, it calls bus.register(agent_id) which creates a private
     FIFO queue (mailbox) for that agent.
  2. When agent A wants to message agent B, it calls bus.send(msg).
     The bus looks up B's mailbox and drops the message in.
  3. Agent B calls bus.receive(agent_id) which blocks until a message arrives
     in its mailbox, then returns it.

THREAD SAFETY:
  Multiple agents run in separate threads simultaneously.  The bus uses a
  threading.Lock() to prevent two threads from modifying the mailbox dictionary
  at the same time (which could corrupt it).
"""

import queue       # Python's built-in thread-safe FIFO queue
import threading   # provides Lock for mutual exclusion
import logging
from typing import Dict, Optional

from core.message import Message

logger = logging.getLogger(__name__)


class MessageBus:
    """
    Singleton thread-safe message router.

    Every agent gets its own queue.Queue (mailbox).
    Sending a message is O(1) — just append to the target queue.
    Receiving blocks the caller's thread until a message arrives (or times out).
    """

    # Class-level variables shared across ALL instances (of which there is only one)
    _instance   = None               # holds the single MessageBus object
    _class_lock = threading.Lock()   # protects _instance creation against race conditions

    def __new__(cls):
        """
        __new__ runs before __init__ every time someone writes MessageBus().
        We override it so that the second, third, Nth call all return the SAME object
        that was created on the first call — the Singleton pattern.

        Double-checked locking:
          First check (without lock) is cheap and handles the common case.
          Second check (inside lock) handles the race where two threads both
          saw _instance as None and are both trying to create the object.
        """
        if cls._instance is None:                       # fast path — no lock needed
            with cls._class_lock:                       # slow path — take the lock
                if cls._instance is None:               # double-check inside the lock
                    inst = super().__new__(cls)          # actually allocate the object
                    inst._mailboxes: Dict[str, queue.Queue] = {}  # agent_id → queue
                    inst._mb_lock = threading.Lock()    # protects _mailboxes dict
                    cls._instance = inst                # store for future calls
        return cls._instance                            # always return the same object

    @classmethod
    def reset(cls):
        """Destroy the singleton — used in tests to start fresh."""
        with cls._class_lock:
            cls._instance = None

    # ------------------------------------------------------------------
    # Mailbox management
    # ------------------------------------------------------------------

    def register(self, agent_id: str):
        """
        Create a mailbox for a new agent.
        Called by BaseAgent.__init__ as soon as an agent object is created.
        queue.Queue is already thread-safe internally, but we lock the dict
        that maps agent_ids to queues.
        """
        with self._mb_lock:
            if agent_id not in self._mailboxes:
                self._mailboxes[agent_id] = queue.Queue()  # unlimited-size FIFO queue

    def deregister(self, agent_id: str):
        """Remove the mailbox when an agent shuts down."""
        with self._mb_lock:
            self._mailboxes.pop(agent_id, None)  # pop with default — no error if missing

    # ------------------------------------------------------------------
    # Sending and receiving
    # ------------------------------------------------------------------

    def send(self, msg: Message) -> bool:
        """
        Drop a message into the receiver's mailbox.

        We hold the lock only while looking up the queue object, then release it
        before calling queue.put().  This keeps the critical section tiny — queue.put()
        itself is thread-safe and could block (though our queues are unlimited so it
        never will).

        Returns True if delivered, False if the receiver has no mailbox.
        """
        with self._mb_lock:
            box = self._mailboxes.get(msg.receiver)   # find the target mailbox

        if box is not None:
            box.put(msg)   # enqueue — thread-safe, never blocks (unlimited queue)
            logger.debug("[BUS] %s -> %s  (%s)", msg.sender, msg.receiver, msg.performative)
            return True

        # Receiver not registered — message is dropped
        logger.warning("[BUS] No mailbox for '%s' (from %s)", msg.receiver, msg.sender)
        return False

    def receive(self, agent_id: str, timeout: Optional[float] = None) -> Optional[Message]:
        """
        Pop the next message from agent_id's mailbox and return it.

        Blocks the caller's thread until:
          - a message arrives (returns the Message), OR
          - timeout seconds elapse (returns None).

        If timeout is None, block forever (rarely used here).
        """
        with self._mb_lock:
            box = self._mailboxes.get(agent_id)   # find this agent's mailbox

        if box is None:
            return None   # agent not registered

        try:
            return box.get(timeout=timeout)   # blocks until message or timeout
        except queue.Empty:
            return None   # timeout expired — no message arrived
