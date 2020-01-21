# -*- coding: utf-8 -*-
#  Copyright © 2019 StarrFox
#
#  Discord Chan is free software: you can redistribute it and/or modify
#  it under the terms of the GNU Affero General Public License as published
#  by the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  Discord Chan is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU Affero General Public License for more details.
#
#  You should have received a copy of the GNU Affero General Public License
#  along with Discord Chan.  If not, see <https://www.gnu.org/licenses/>.

import logging
import pathlib
import typing
from collections import defaultdict
from configparser import ConfigParser
from datetime import datetime
from typing import Optional

import discord
from discord.ext import commands, tasks

from .context import SubContext
from .help import Minimal
from . import db

logger = logging.getLogger(__name__)


class DiscordChan(commands.AutoShardedBot):

    def __init__(self, config: ConfigParser, *, context: commands.Context = SubContext, **kwargs):
        super().__init__(
            command_prefix=kwargs.pop('command_prefix', self.get_prefix),
            case_insensitive=kwargs.pop('case_insensitive', True),
            max_messages=kwargs.pop('max_messages', 10_000),
            help_command=kwargs.pop('help_command', Minimal()),
            **kwargs
        )
        self.config = config
        self.context = context
        self.uptime = datetime.utcnow()
        self.prefixes = defaultdict(lambda: {config['general']['prefix']})
        self.presence_cycle.start()  # pylint: disable=no-member
        self.ready_once = False

    def get_message(self, message_id: int) -> Optional[discord.Message]:
        """
        Gets a message from cache
        """
        return discord.utils.get(
            self.cached_messages,
            id=message_id
        )

    async def process_commands(self, message):
        if message.author.bot:
            return

        ctx = await self.get_context(message, cls=self.context)

        await self.invoke(ctx)

    async def on_message_edit(self, before, after):
        if before.content != after.content:
            await self.process_commands(after)

    async def on_ready(self):
        if self.ready_once:
            return

        self.ready_once = True

        await self.load_prefixes()

        if self.config['general'].getboolean('load_extensions'):
            self.load_extensions_from_dir('discord_chan/extensions')

        logger.info(f'Bot ready with {len(self.extensions.keys())} extensions.')

    # # Todo: remove before going into prod
    # async def start(self, *args, **kwargs):
    #     # Todo: uncomment to run
    #     # await super().start(*args, **kwargs)
    #
    #     #  Temp replacement for self.connect
    #     import asyncio
    #     while not self.is_closed():
    #         await asyncio.sleep(100)

    def run(self, *args, **kwargs):
        return super().run(self.config['discord']['token'], *args, **kwargs)

    def load_extensions_from_dir(self, path: typing.Union[str, pathlib.Path]) -> int:
        """
        Loads any python files in a directory and it's children
        as extensions

        :param path: Path to directory to load
        :return: Number of extensions loaded
        """
        if not isinstance(path, pathlib.Path):
            path = pathlib.Path(path)

        if not path.is_dir():
            return 0

        before = len(self.extensions.keys())

        extension_names = []

        for subpath in path.glob('**/[!_]*.py'):  # Ignore if starts with _

            parts = subpath.with_suffix('').parts
            if parts[0] == '.':
                parts = parts[1:]

            extension_names.append('.'.join(parts))

        for ext in extension_names:
            try:
                self.load_extension(ext)
            except (commands.errors.ExtensionError, commands.errors.ExtensionFailed):
                logger.error('Failed loading ' + ext, exc_info=True)

        return len(self.extensions.keys()) - before

    @tasks.loop(hours=5)
    async def presence_cycle(self):
        """
        Keeps the status message active
        """
        await self.change_presence(activity=discord.Activity(
            type=discord.ActivityType.listening,
            name=f"{self.config['general']['prefix']}help"
        ))

    @presence_cycle.before_loop
    async def presence_cycle_before(self):
        await self.wait_until_ready()

    @presence_cycle.after_loop
    async def presence_cycle_after(self):
        if self.presence_cycle.failed():
            # Only here because it somehow had an error once
            logger.error('Presence cycle somehow errored out, restarting.', exc_info=True)
            self.presence_cycle.restart()

    async def get_prefix(self, message: discord.Message):
        if message.guild:
            return commands.when_mentioned_or(*self.prefixes[message.guild.id])(self, message)
        else:  # DM
            return commands.when_mentioned_or(self.config['general']['prefix'], '')(self, message)

    async def load_prefixes(self):
        async with db.get_database() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute("SELECT * FROM prefixes;")
                for guild_id, prefixes in await cursor.fetchall():
                    self.prefixes[guild_id] = prefixes

        logger.info(f"Loaded prefixes for {len(self.prefixes)} guilds.")

    async def unload_prefixes(self):
        async with db.get_database() as connection:
            async with connection.cursor() as cursor:
                await cursor.executemany(
                    "INSERT INTO prefixes (guild_id, prefixes) VALUES (?, ?) "
                    "ON CONFLICT (guild_id) DO UPDATE SET prefixes = EXCLUDED.prefixes",
                    self.prefixes.items()
                )
            await connection.commit()

        logger.info(f"Unloaded prefixes for {len(self.prefixes)} guilds.")