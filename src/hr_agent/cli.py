from __future__ import annotations

import argparse
import asyncio

from hr_agent.telemetry import setup_telemetry
from hr_agent.agents.hr_agent import ask


async def _main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("question", help="Question to ask the HR agent")
    parser.add_argument("--no-reuse-thread", action="store_true", help="Do not reuse persisted thread_id")
    parser.add_argument("--thread-id", default=None, help="Explicit thread id to use")
    parser.add_argument("--stream", action="store_true", help="Stream the agent response")
    args = parser.parse_args()

    setup_telemetry()

    answer, tid = await ask(
        question=args.question,
        thread_id=args.thread_id,
        reuse_thread=(not args.no_reuse_thread),
        stream=args.stream,
    )

    print("\n\n=== ANSWER ===\n")
    print(answer)
    if tid:
        print(f"\n[thread_id] {tid}")


if __name__ == "__main__":
    asyncio.run(_main())
