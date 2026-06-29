"""
agents/base_agent.py — the shared foundation that every agent inherits from.

ROLE IN THE SYSTEM:
  All five agent types (Crane, Source, Process, Part, Sink) extend this class.
  It gives them:
    1. A unique identity (agent_id)
    2. A mailbox on the MessageBus
    3. A background thread that runs their _run() loop
    4. Helper methods to send and receive messages
    5. A receive_from() method that waits for a message from a specific sender
       without losing messages from other senders

  Subclasses only need to implement _run() — everything else is handled here.
"""

import threading    # Python's standard thread library
import logging      # for structured log output
import time         # used for deadline calculations in receive_from()
from typing import List, Optional

from core.message import Message
from core.message_bus import MessageBus


class BaseAgent:
    """
    Template for all agents in the system.

    Lifecycle:
      __init__()  — create the agent object and register its mailbox
      start()     — spawn a daemon thread and begin running _run()
      _run()      — the agent's main loop (implemented by each subclass)
      stop()      — set _running=False so the loop exits, then join the thread
    """

    def __init__(self, agent_id: str):
        """
        Set up the agent's identity, mailbox, and logger.

        agent_id  — a unique string name like "Crane", "Process1", "Part_Source1_2".
                    This is used as:
                      - the key in the MessageBus mailbox dict
                      - the sender/receiver fields in every Message
                      - the Python logging channel name
        """
        self.agent_id = agent_id                    # unique name — doubles as routing key
        self.bus      = MessageBus()                # access the singleton bus
        self.bus.register(agent_id)                 # create this agent's mailbox queue
        self._running = False                       # controls whether _run() keeps looping
        self._thread: Optional[threading.Thread] = None   # the background thread (set on start())
        self.logger   = logging.getLogger(self.agent_id)  # logs show up as "CraneAgent  INFO ..."

    # ------------------------------------------------------------------
    # Lifecycle — called from main.py to start and stop agents
    # ------------------------------------------------------------------

    def start(self):
        """
        Spawn a background thread and begin the agent's main loop.

        daemon=True means Python will kill this thread automatically when the
        main program exits — we don't have to manually stop every thread on exit.

        The thread runs _run() from the subclass (CraneAgent._run(), etc.).
        """
        self._running = True
        self._thread  = threading.Thread(
            target=self._run,       # function to run in the new thread
            name=self.agent_id,     # thread name shown in debuggers
            daemon=True,            # auto-killed when main thread exits
        )
        self._thread.start()        # actually start the thread
        self.logger.info("Agent started")

    def stop(self):
        """
        Signal the agent to stop and wait up to 5 seconds for it to finish.

        Setting _running=False causes the while-loop in _run() to exit on its
        next iteration.  join() then waits for the thread to actually finish.
        After that, the mailbox is removed from the bus.
        """
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)   # wait at most 5 seconds
        self.bus.deregister(self.agent_id)   # clean up the mailbox
        self.logger.info("Agent stopped")

    def is_alive(self) -> bool:
        """Return True if the agent's background thread is still running."""
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------
    # Messaging helpers — thin wrappers around the MessageBus
    # ------------------------------------------------------------------

    def send(self, msg: Message):
        """
        Put a message onto the bus so it reaches the receiver's mailbox.
        Non-blocking — returns immediately after enqueuing.
        """
        self.bus.send(msg)

    def receive(self, timeout: float = 1.0) -> Optional[Message]:
        """
        Pop the next message from THIS agent's mailbox.

        Blocks for up to `timeout` seconds.  Returns None on timeout.
        Callers typically call this in a loop:

            while self._running:
                msg = self.receive(timeout=1.0)
                if msg is None:
                    continue          # nothing arrived yet — loop again
                self._handle(msg)
        """
        return self.bus.receive(self.agent_id, timeout=timeout)

    def receive_from(self, sender_id: str, timeout: float = 1600.0) -> Optional[Message]:
        """
        Block until a message from a SPECIFIC sender arrives.

        Why do we need this?
          In a busy system multiple agents send messages to the same receiver.
          A plain receive() would return whatever arrives first, which might be
          from the wrong agent at the wrong time.

          receive_from() solves this by:
            1. Calling receive() repeatedly.
            2. If the message is from the wrong sender, saving it in `pending`.
            3. When the right message arrives, re-queuing all `pending` messages
               so they are still available for later receive() calls.

        This is why agents never miss messages — they are buffered, not discarded.

        Returns the first message from sender_id, or None on timeout.
        """
        pending: List[Message] = []            # messages from other senders, held temporarily
        deadline = time.time() + timeout       # absolute time when we give up

        while time.time() < deadline:
            remaining = deadline - time.time()                 # how long left to wait
            msg = self.receive(timeout=min(0.5, remaining))    # short poll so we can re-check deadline
            if msg is None:
                continue                        # nothing arrived yet — try again
            if msg.sender == sender_id:
                # Found the message we were waiting for.
                # Put back any messages we skipped over so other receive() calls can see them.
                for m in pending:
                    self.bus.send(m)
                return msg                      # return the target message
            # Wrong sender — save it and keep looking
            pending.append(msg)

        # Timed out — put back everything we buffered
        for m in pending:
            self.bus.send(m)
        self.logger.warning("Timeout waiting for message from %s", sender_id)
        return None

    # ------------------------------------------------------------------
    # Main loop — every subclass MUST override this
    # ------------------------------------------------------------------

    def _run(self):
        """
        The agent's main behaviour loop.  Runs in a background thread.

        Subclasses implement this method.  The typical pattern is:

            def _run(self):
                # 1. Register capabilities in the Directory Facilitator
                # 2. Loop while self._running:
                #      msg = self.receive(timeout=1.0)
                #      if msg: handle it
        """
        raise NotImplementedError   # forces every subclass to provide its own _run()
