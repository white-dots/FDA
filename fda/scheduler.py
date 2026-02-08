"""
Scheduler for recurring tasks and event monitoring.

Uses threading.Timer for in-process scheduling of periodic checks.
"""

import threading
import logging
from datetime import datetime, timedelta
from typing import Callable, Optional, Any

from fda.config import (
    DEFAULT_DAILY_CHECKIN_TIME,
    DEFAULT_CALENDAR_CHECK_INTERVAL_MINUTES,
)

logger = logging.getLogger(__name__)


class Scheduler:
    """
    In-process scheduler for recurring tasks.

    Uses threading.Timer to schedule periodic callbacks.
    """

    def __init__(self):
        """Initialize the scheduler."""
        self.timers: dict[str, threading.Timer] = {}
        self.tasks: dict[str, dict[str, Any]] = {}
        self._running = False
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

    def register_daily_checkin(
        self,
        time: str = DEFAULT_DAILY_CHECKIN_TIME,
        callback: Optional[Callable[[], None]] = None,
    ) -> None:
        """
        Register a daily checkin task at a specific time.

        Args:
            time: Time in HH:MM format (24-hour).
            callback: Function to call at checkin time.
        """
        hour, minute = map(int, time.split(":"))

        def schedule_next():
            if not self._running:
                return

            now = datetime.now()
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

            # If the time already passed today, schedule for tomorrow
            if target <= now:
                target += timedelta(days=1)

            delay = (target - now).total_seconds()

            def run_and_reschedule():
                if not self._running:
                    return
                try:
                    if callback:
                        callback()
                    else:
                        logger.info(f"Daily checkin triggered at {datetime.now()}")
                except Exception as e:
                    logger.error(f"Error in daily checkin: {e}")
                # Schedule next occurrence
                schedule_next()

            with self._lock:
                if "daily_checkin" in self.timers:
                    self.timers["daily_checkin"].cancel()
                timer = threading.Timer(delay, run_and_reschedule)
                timer.daemon = True
                self.timers["daily_checkin"] = timer
                if self._running:
                    timer.start()
                    logger.info(f"Daily checkin scheduled for {target}")

        self.tasks["daily_checkin"] = {
            "type": "daily",
            "time": time,
            "callback": callback,
            "schedule_func": schedule_next,
        }

    def register_calendar_watcher(
        self,
        interval_min: int = DEFAULT_CALENDAR_CHECK_INTERVAL_MINUTES,
        callback: Optional[Callable[[], None]] = None,
    ) -> None:
        """
        Register periodic calendar watching.

        Args:
            interval_min: Interval in minutes between checks.
            callback: Function to call on each check.
        """
        self.register_task(
            name="calendar_watcher",
            callback=callback or (lambda: logger.info("Calendar check triggered")),
            interval_seconds=interval_min * 60,
        )

    def register_task(
        self,
        name: str,
        callback: Callable[[], None],
        interval_seconds: int,
    ) -> None:
        """
        Register a periodic task.

        Args:
            name: Name of the task.
            callback: Function to call periodically.
            interval_seconds: Interval between calls in seconds.
        """

        def schedule_next():
            if not self._running:
                return

            def run_and_reschedule():
                if not self._running:
                    return
                try:
                    callback()
                except Exception as e:
                    logger.error(f"Error in task '{name}': {e}")
                # Schedule next occurrence
                schedule_next()

            with self._lock:
                if name in self.timers:
                    self.timers[name].cancel()
                timer = threading.Timer(interval_seconds, run_and_reschedule)
                timer.daemon = True
                self.timers[name] = timer
                if self._running:
                    timer.start()

        self.tasks[name] = {
            "type": "periodic",
            "interval_seconds": interval_seconds,
            "callback": callback,
            "schedule_func": schedule_next,
        }

    def register_one_time(
        self,
        name: str,
        callback: Callable[[], None],
        delay_seconds: float,
    ) -> None:
        """
        Register a one-time task to run after a delay.

        Args:
            name: Name of the task.
            callback: Function to call.
            delay_seconds: Delay in seconds before calling.
        """

        def run_task():
            if not self._running:
                return
            try:
                callback()
            except Exception as e:
                logger.error(f"Error in one-time task '{name}': {e}")
            finally:
                with self._lock:
                    if name in self.timers:
                        del self.timers[name]
                    if name in self.tasks:
                        del self.tasks[name]

        with self._lock:
            timer = threading.Timer(delay_seconds, run_task)
            timer.daemon = True
            self.timers[name] = timer
            self.tasks[name] = {
                "type": "one_time",
                "delay_seconds": delay_seconds,
                "callback": callback,
            }
            if self._running:
                timer.start()

    def unregister_task(self, name: str) -> bool:
        """
        Unregister a task by name.

        Args:
            name: Name of the task to unregister.

        Returns:
            True if task was found and removed, False otherwise.
        """
        with self._lock:
            if name in self.timers:
                self.timers[name].cancel()
                del self.timers[name]
            if name in self.tasks:
                del self.tasks[name]
                return True
            return False

    def run(self) -> None:
        """
        Start the scheduler event loop.

        Blocks until stop() is called.
        """
        self._running = True
        self._stop_event.clear()
        logger.info("Scheduler starting...")

        # Start all registered tasks
        with self._lock:
            for name, task in self.tasks.items():
                if "schedule_func" in task:
                    task["schedule_func"]()
                elif task["type"] == "one_time":
                    self.timers[name].start()

        logger.info(f"Scheduler running with {len(self.tasks)} tasks")

        # Block until stop is called
        self._stop_event.wait()
        logger.info("Scheduler stopped")

    def run_in_background(self) -> threading.Thread:
        """
        Start the scheduler in a background thread.

        Returns:
            The background thread running the scheduler.
        """
        thread = threading.Thread(target=self.run, daemon=True)
        thread.start()
        return thread

    def stop(self) -> None:
        """
        Stop the scheduler and cancel all pending tasks.
        """
        logger.info("Stopping scheduler...")
        self._running = False

        with self._lock:
            for name, timer in list(self.timers.items()):
                timer.cancel()
            self.timers.clear()

        self._stop_event.set()

    def get_status(self) -> dict[str, Any]:
        """
        Get the current status of the scheduler.

        Returns:
            Dictionary with scheduler status information.
        """
        with self._lock:
            return {
                "running": self._running,
                "task_count": len(self.tasks),
                "tasks": {
                    name: {
                        "type": task["type"],
                        "interval_seconds": task.get("interval_seconds"),
                        "time": task.get("time"),
                    }
                    for name, task in self.tasks.items()
                },
            }
