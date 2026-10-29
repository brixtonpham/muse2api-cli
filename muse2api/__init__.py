"""Unofficial Python library and command line for Muse (muse.ai): generate videos and images from a prompt plus
reference images, in a browser logged in to your own account.

    import asyncio, muse2api

    async def main():
        async with muse2api.Muse("main") as m:
            g = await m.generate("a 10 s video of a cat surfing", refs=["cat.jpg"], out="out/")
            print(g.ok, [f.file for f in g.media], g.usage.weekly_pct)

    asyncio.run(main())
"""
from ._core import Store
from .client import (Generation, LimitReached, MediaFile, Muse, MuseError, Turn, Usage, accounts, generate,
                     login_browser, login_from_chrome, login_from_file, parse_usage)

__version__ = "0.2.0"
__all__ = ["Muse", "Generation", "Turn", "MediaFile", "Usage", "Store", "MuseError", "LimitReached", "generate",
           "accounts", "login_from_file", "login_from_chrome", "login_browser", "parse_usage", "__version__"]
