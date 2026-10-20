"""Compatibility entrypoint for the complete isolated live validation."""
import asyncio
from tools.live_validate import main

if __name__ == "__main__":
    asyncio.run(main())
