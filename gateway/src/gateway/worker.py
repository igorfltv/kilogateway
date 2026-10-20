"""Separate durable-job worker. PostgreSQL elects one job pass across replicas."""
import asyncio
import signal

from gateway.app import create_app


async def main():
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopped.set)
    app = create_app(worker=False)
    async with app.router.lifespan_context(app):
        work = asyncio.create_task(app.state.broker.worker())
        waiter = asyncio.create_task(stopped.wait())
        try:
            await asyncio.wait([work, waiter], return_when=asyncio.FIRST_COMPLETED)
            if work.done():
                await work
        finally:
            work.cancel()
            waiter.cancel()
            await asyncio.gather(work, waiter, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
