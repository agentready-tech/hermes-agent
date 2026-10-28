"""Concurrent native tool workers, start-order gate and deadline handling."""

from __future__ import annotations

import concurrent.futures
import contextlib
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

from agent import tool_executor as te
from tools.thread_context import propagate_context_to_thread
from agent.tool_operation_lifecycle import execution_agent, incoming_event, prepare_operation

logger = logging.getLogger(__name__)


@dataclass
class _ToolOutcome:
    """One finished worker slot of a concurrent batch (``ref`` holds the final name/args/trace)."""

    ref: te._ToolCallRef
    result: Any
    duration: float
    is_error: bool
    blocked: bool


def _start_order_gate_timeout(batch_timeout: float | None) -> float:
    """The gate bound must sit UNDER the batch deadline, else parked workers are falsely
    reported timed out without starting. A disabled deadline keeps the stock bound."""
    if batch_timeout is None:
        return te._START_ORDER_GATE_TIMEOUT_S
    return min(te._START_ORDER_GATE_TIMEOUT_S, batch_timeout / 2)


class _StartOrderGate:
    """Serialize worker dispatch by submit order (prompts appear in call order); ``abandon()``
    releases every parked worker so none dispatches a tool the turn already gave up on."""

    def __init__(self, timeout: float) -> None:
        self._condition = threading.Condition()
        self._next_order = 0
        self._timeout = timeout
        self.abandoned = threading.Event()

    def abandon(self) -> None:
        self.abandoned.set()
        with self._condition:
            self._condition.notify_all()

    def begin_in_order(self, order: int, callback=None, *, tool_name: str = "") -> bool:
        """Wait for ``order``, run ``callback``, advance. Returns False if abandoned."""
        with self._condition:
            # Bounded wait so one wedged dispatch can't starve later-ordered workers; on
            # expiry proceed out of order (interleaved prompts beat starvation). ``>=`` (not
            # ``==``) releases every skipped worker at once; abandoned short-circuits.
            in_order = self._condition.wait_for(
                lambda: self._next_order >= order or self.abandoned.is_set(), timeout=self._timeout,
            )
            if self.abandoned.is_set():
                return False  # the turn already synthesized this result; don't advance
            if not in_order:
                logger.warning(
                    "start-order gate timed out for %s (order=%d next=%d); proceeding out of order",
                    tool_name or "tool", order, self._next_order,
                )
            try:
                if callback is not None:
                    callback()
            finally:
                self._next_order = max(self._next_order, order + 1)
                self._condition.notify_all()
        return True


class _WorkerStartOnce:
    """One worker's handle on the start-order gate: advances at most once, raising
    ``te._BatchAbandoned`` (instead of dispatching late) when the batch was abandoned."""

    def __init__(self, gate: _StartOrderGate, order: int, tool_name: str) -> None:
        self._gate, self._order, self._tool_name, self._advanced = gate, order, tool_name, False

    def advance(self, callback=None) -> None:
        if self._advanced:
            return
        self._advanced = True
        if not self._gate.begin_in_order(self._order, callback, tool_name=self._tool_name):
            raise te._BatchAbandoned(self._tool_name)


class ConcurrentBatch:
    """Shared state of one concurrent tool batch: per-slot results, the start-order and
    authorization gates, and the deadline bookkeeping the wait loop needs."""

    def __init__(self, agent, messages: list, effective_task_id: str, parsed_calls: list[te._ParsedCall], timeout_s: float | None) -> None:
        self.agent = agent
        self.messages = messages
        self.effective_task_id = effective_task_id
        self.parsed_calls = parsed_calls
        self.timeout_s = timeout_s
        self.results: list[Optional[_ToolOutcome]] = [None] * len(parsed_calls)
        for i, pc in enumerate(parsed_calls):
            if pc.parse_error is not None:
                self.results[i] = _ToolOutcome(pc.ref(effective_task_id), pc.parse_error, 0.0, True, True)
        self.gate = _StartOrderGate(_start_order_gate_timeout(timeout_s))
        self.authorization_gate = te._ConcurrentToolAuthorizationGate()
        self.timed_out_indices: set[int] = set()
        self.operations = {i: prepare_operation(agent, pc.name, pc.args, effective_task_id, pc.ref(effective_task_id).call_id)
            for i, pc in enumerate(parsed_calls) if pc.parse_error is None}
        self.deferred = False
        self.defer_status = "running"
        self.results_lock = threading.Lock()
        self.detached_indices: set[int] = set()

    def _dispatch_worker(self, index: int, ref: te._ToolCallRef, scope_block, start_gate: _WorkerStartOnce) -> Optional[_ToolOutcome]:
        """Run one call through the middleware and synthesize its slot outcome; ``None`` when
        abandoned at the gate (the main thread already wrote this slot; emitting would
        double-report the tool_call_id)."""
        operation = self.operations.get(index)
        agent = execution_agent(self.agent, operation)
        if operation is not None and (replayed := operation.replay()) is not None:
            return _ToolOutcome(ref, replayed, 0.0, False, True)
        # Approval/sudo callbacks (thread-local) and the agent turn's ContextVars are propagated by
        # propagate_context_to_thread() at the submit site below (GHSA-qg5c-hvr5-hjgr, #13617).
        start = time.time()
        blocked = dispatched = False
        try:
            managed = te._run_agent_tool_execution_middleware(
                agent,
                **ref.middleware_kwargs(),
                execute=lambda next_args: agent._invoke_tool(
                    ref.name, next_args, ref.task_id, ref.call_id,
                    messages=self.messages if operation is None else None,
                    pre_tool_block_checked=True,
                    skip_tool_request_middleware=True,
                    skip_tool_execution_middleware=True,
                    tool_request_middleware_trace=list(ref.trace),
                ),
                scope_block=scope_block,
                display_index=index + 1,
                begin_execution=start_gate.advance,
                authorization_gate=self.authorization_gate,
            )
            result, ref.args, ref.trace = managed.result, managed.args, managed.middleware_trace
            blocked, dispatched = managed.blocked, managed.dispatched
        except te._BatchAbandoned:
            logger.info("tool %s abandoned at start-order gate; skipping dispatch", ref.name)
            if operation is not None:
                result = ref.emit_cancelled(agent, start)
                operation.complete(result)
                return _ToolOutcome(ref, result, time.time() - start, True, True)
            return None
        except KeyboardInterrupt:
            with contextlib.suppress(Exception):
                agent.interrupt("keyboard interrupt", tool_reason=te._REASON_USER_INTERRUPT)
            result = ref.emit_cancelled(agent, start)
            if operation is not None:
                operation.complete(result)
            duration = time.time() - start
            logger.info("tool %s cancelled (%.2fs)", ref.name, duration)
            return _ToolOutcome(ref, result, duration, True, False)
        except Exception as tool_error:
            result = f"Error executing tool '{ref.name}': {tool_error}"
            logger.error("_invoke_tool raised for %s: %s", ref.name, tool_error, exc_info=True)
        duration = time.time() - start
        if not blocked and not dispatched:
            ref.emit_post(agent, result, duration_ms=int(duration * 1000))
        is_error, _ = te._detect_tool_failure(ref.name, result)
        if is_error:
            logger.info("tool %s failed (%.2fs): %s", ref.name, duration, str(result)[:200])
        else:
            result_chars = len(result) if isinstance(result, str) else len(str(result))
            logger.info(
                "tool %s completed (%.2fs, %d chars)", ref.name, duration, result_chars
            )
        if operation is not None:
            operation.complete(result)
        return _ToolOutcome(ref, result, duration, is_error, blocked)

    def run_worker(self, index: int, start_order: int) -> None:
        """Worker function executed in a thread."""
        agent, pc = self.agent, self.parsed_calls[index]
        with te._registered_tool_worker(agent) as _worker_tid:
            # An interrupt may have fanned out before our registration; apply it to our tid.
            if agent._interrupt_requested:
                te._interrupt_worker_tids(agent, [_worker_tid], reason=getattr(agent, "_tool_interrupt_reason", None))
            te._set_worker_activity_callback(agent)
            start_gate = _WorkerStartOnce(self.gate, start_order, pc.name)
            try:
                outcome = self._dispatch_worker(index, pc.ref(self.effective_task_id), pc.scope_block, start_gate)
                if outcome is not None:
                    with self.results_lock:
                        if index not in self.detached_indices:
                            self.results[index] = outcome
            finally:
                with contextlib.suppress(te._BatchAbandoned):
                    start_gate.advance()  # keep later-ordered workers moving

    def submit_all(self, executor, runnable: list[int]) -> tuple[list, dict]:
        """Submit every runnable slot; on interpreter shutdown, synthesize error results
        for the unsubmitted remainder instead of raising. ``propagate_context_to_thread``
        carries turn ContextVars and thread-local approval/sudo callbacks into the worker."""
        futures = []
        future_to_index = {}
        for submit_index, i in enumerate(runnable):
            try:
                f = executor.submit(propagate_context_to_thread(self.run_worker), i, submit_index)
            except RuntimeError as submit_error:
                if not te._is_interpreter_shutdown_submit_error(submit_error):
                    raise
                skipped = runnable[submit_index:]
                logger.warning(
                    "interpreter shutdown while scheduling concurrent tools; skipping %d unsubmitted tool(s)", len(skipped),
                )
                for skipped_i in skipped:
                    ref = self.parsed_calls[skipped_i].ref(self.effective_task_id)
                    if self.results[skipped_i] is None:
                        result = f"Error executing tool '{ref.name}': Python interpreter is shutting down; tool was not started"
                        if operation := self.operations.get(skipped_i):
                            operation.complete(result)
                        self.results[skipped_i] = _ToolOutcome(ref, result, 0.0, True, False)
                break
            futures.append(f)
            future_to_index[f] = i
        return futures, future_to_index

    def _running_names(self, not_done, future_to_index) -> list[str]:
        return [self.parsed_calls[future_to_index[f]].name for f in not_done if f in future_to_index]

    def await_completion(self, futures, future_to_index, deadline: float | None) -> bool:
        """Wait with periodic heartbeats and interrupt checks; True when the batch was
        abandoned (deadline or interrupt) and the executor must not join its workers."""
        agent = self.agent
        _conc_start = time.time()
        while True:
            wait_timeout = 0.2 if any(self.operations.values()) else 5.0
            if deadline is not None:
                remaining = deadline + self.authorization_gate.excluded_seconds() - time.monotonic()
                if remaining <= 0:
                    not_done = {f for f in futures if not f.done()}
                else:
                    wait_timeout = min(wait_timeout, remaining)
            if deadline is None or remaining > 0:
                _done, not_done = concurrent.futures.wait(futures, timeout=wait_timeout)
            if not not_done:
                return False

            if incoming_event(agent) and all(self.operations.get(future_to_index[f]) is not None for f in not_done):
                self.deferred = True
                return True

            timed_out = deadline is not None and time.monotonic() >= deadline + self.authorization_gate.excluded_seconds()
            if (timed_out or agent._interrupt_requested) and all(self.operations.get(future_to_index[f]) is not None for f in not_done):
                if agent._interrupt_requested:
                    # A stop prevents queued/parked work from starting. Running
                    # non-cooperative calls keep an unknown outcome until they finish.
                    self.gate.abandon()
                    for future in not_done:
                        if future.cancel():
                            index = future_to_index[future]
                            ref = self.parsed_calls[index].ref(self.effective_task_id)
                            result = ref.emit_cancelled(agent, time.time())
                            self.operations[index].complete(result)
                            self.results[index] = _ToolOutcome(ref, result, 0.0, True, True)
                self.deferred, self.defer_status = True, "outcome_unknown"
                return True
            if timed_out:
                self.timed_out_indices = {future_to_index[f] for f in not_done if f in future_to_index}
                logger.warning(
                    "concurrent tool batch timed out after %.1fs; %d tool(s) still running: %s",
                    self.timeout_s,
                    len(self.timed_out_indices),
                    ", ".join(self._running_names(not_done, future_to_index)[:5]),
                )
            elif agent._interrupt_requested:
                # Tools without interrupt checks (web_search, read_file) run to
                # completion; cancel unstarted futures so we don't block on them.
                agent._vprint(
                    f"{agent.log_prefix}⚡ Interrupt: cancelling {len(not_done)} pending concurrent tool(s)",
                    force=True,
                )
            else:
                _conc_elapsed = int(time.time() - _conc_start)
                # Heartbeat every ~30s (6 × 5s poll intervals)
                if _conc_elapsed > 0 and _conc_elapsed % 30 < 6:
                    _still_running = self._running_names(not_done, future_to_index)
                    agent._touch_activity(
                        f"concurrent tools running ({_conc_elapsed}s, "
                        f"{len(not_done)} remaining: {', '.join(_still_running[:3])})"
                    )
                continue
            for f in not_done:
                f.cancel()
            # Release gate-parked workers BEFORE interrupt fan-out so none later
            # dispatches a tool the turn already reported as timed out / interrupted.
            self.gate.abandon()
            if timed_out:
                with agent._tool_worker_threads_lock:
                    worker_tids = list(agent._tool_worker_threads)
                te._interrupt_worker_tids(agent, worker_tids)
            else:
                # Give running tools a moment to notice the per-thread interrupt and exit gracefully.
                concurrent.futures.wait(not_done, timeout=3.0)
            return True

    def run(self) -> None:
        """Dispatch the runnable calls on a daemon pool and wait for the batch."""
        runnable = [i for i, pc in enumerate(self.parsed_calls) if pc.parse_error is None]
        if not runnable:
            return
        deadline = time.monotonic() + self.timeout_s if self.timeout_s is not None else None
        max_workers = te._max_workers_for_tool_batch([(i, None, self.parsed_calls[i].name) for i in runnable])
        # Daemon workers: the stdlib pool's atexit join would let one wedged tool block exit.
        from tools.daemon_pool import DaemonThreadPoolExecutor
        executor = DaemonThreadPoolExecutor(max_workers=max_workers)
        abandon_executor = False
        try:
            futures, future_to_index = self.submit_all(executor, runnable)
            abandon_executor = self.await_completion(futures, future_to_index, deadline)
        finally:
            # Every abandoning exit releases gate-parked workers and leaves wedged threads
            # detached rather than joining them; normal completion joins.
            if abandon_executor and not self.deferred:
                self.gate.abandon()
            if self.deferred:
                with self.results_lock:
                    for i, operation in self.operations.items():
                        if operation is not None and self.results[i] is None:
                            # Freeze before the worker can publish its slot.
                            self.detached_indices.add(i)
                            self.results[i] = _ToolOutcome(self.parsed_calls[i].ref(self.effective_task_id),
                                operation.defer(self.defer_status), 0.0, False, False)
            executor.shutdown(wait=not abandon_executor, cancel_futures=abandon_executor and not self.deferred)
