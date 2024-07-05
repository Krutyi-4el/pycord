"""
The MIT License (MIT)

Copyright (c) 2015-2021 Rapptz
Copyright (c) 2021-present Pycord Development

Permission is hereby granted, free of charge, to any person obtaining a
copy of this software and associated documentation files (the "Software"),
to deal in the Software without restriction, including without limitation
the rights to use, copy, modify, merge, publish, distribute, sublicense,
and/or sell copies of the Software, and to permit persons to whom the
Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
DEALINGS IN THE SOFTWARE.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import itertools
import logging
import os
from collections import OrderedDict, deque
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Coroutine,
    Deque,
    Sequence,
    TypeVar,
    Union,
)

from . import utils
from .activity import BaseActivity
from .audit_logs import AuditLogEntry
from .automod import AutoModRule
from .channel import *
from .channel import _channel_factory
from .emoji import Emoji
from .enums import ChannelType, InteractionType, ScheduledEventStatus, Status, try_enum
from .flags import ApplicationFlags, Intents, MemberCacheFlags
from .guild import Guild
from .integrations import _integration_factory
from .interactions import Interaction
from .invite import Invite
from .member import Member
from .mentions import AllowedMentions
from .message import Message
from .monetization import Entitlement
from .object import Object
from .partial_emoji import PartialEmoji
from .poll import Poll, PollAnswerCount
from .raw_models import *
from .role import Role
from .scheduled_events import ScheduledEvent
from .stage_instance import StageInstance
from .sticker import GuildSticker
from .threads import Thread, ThreadMember
from .ui.modal import Modal, ModalStore
from .ui.view import View, ViewStore
from .user import ClientUser, User

if TYPE_CHECKING:
    from .abc import PrivateChannel
    from .client import Client
    from .gateway import DiscordWebSocket
    from .guild import GuildChannel, VocalGuildChannel
    from .http import HTTPClient
    from .message import MessageableChannel
    from .types.activity import Activity as ActivityPayload
    from .types.channel import DMChannel as DMChannelPayload
    from .types.emoji import Emoji as EmojiPayload
    from .types.guild import Guild as GuildPayload
    from .types.message import Message as MessagePayload
    from .types.poll import Poll as PollPayload
    from .types.sticker import GuildSticker as GuildStickerPayload
    from .types.user import User as UserPayload
    from .voice_client import VoiceClient

    T = TypeVar("T")
    CS = TypeVar("CS", bound="ConnectionState")
    Channel = Union[GuildChannel, VocalGuildChannel, PrivateChannel, PartialMessageable]


class ChunkRequest:
    def __init__(
        self,
        guild_id: int,
        loop: asyncio.AbstractEventLoop,
        resolver: Callable[[int], Any],
        *,
        cache: bool = True,
    ) -> None:
        self.guild_id: int = guild_id
        self.resolver: Callable[[int], Any] = resolver
        self.loop: asyncio.AbstractEventLoop = loop
        self.cache: bool = cache
        self.nonce: str = os.urandom(16).hex()
        self.buffer: list[Member] = []
        self.waiters: list[asyncio.Future[list[Member]]] = []

    def add_members(self, members: list[Member]) -> None:
        self.buffer.extend(members)
        if self.cache:
            guild = self.resolver(self.guild_id)
            if guild is None:
                return

            for member in members:
                existing = guild.get_member(member.id)
                if existing is None or existing.joined_at is None:
                    guild._add_member(member)

    async def wait(self) -> list[Member]:
        future = self.loop.create_future()
        self.waiters.append(future)
        try:
            return await future
        finally:
            self.waiters.remove(future)

    def get_future(self) -> asyncio.Future[list[Member]]:
        future = self.loop.create_future()
        self.waiters.append(future)
        return future

    def done(self) -> None:
        for future in self.waiters:
            if not future.done():
                future.set_result(self.buffer)


_log = logging.getLogger(__name__)


async def logging_coroutine(coroutine: Coroutine[Any, Any, T], *, info: str) -> None:
    try:
        await coroutine
    except Exception:
        _log.exception("Exception occurred during %s", info)


class ConnectionState:
    if TYPE_CHECKING:
        _get_websocket: Callable[..., DiscordWebSocket]
        _get_client: Callable[..., Client]
        _parsers: dict[str, Callable[[dict[str, Any]], None]]

    def __init__(
        self,
        *,
        dispatch: Callable,
        handlers: dict[str, Callable],
        hooks: dict[str, Callable],
        http: HTTPClient,
        loop: asyncio.AbstractEventLoop,
        **options: Any,
    ) -> None:
        self.loop: asyncio.AbstractEventLoop = loop
        self.http: HTTPClient = http
        self.max_messages: int | None = options.get("max_messages", 1000)
        if self.max_messages is not None and self.max_messages <= 0:
            self.max_messages = 1000

        self.dispatch: Callable = dispatch
        self.handlers: dict[str, Callable] = handlers
        self.hooks: dict[str, Callable] = hooks
        self.shard_count: int | None = None
        self._ready_task: asyncio.Task | None = None
        self.application_id: int | None = utils._get_as_snowflake(
            options, "application_id"
        )
        self.heartbeat_timeout: float = options.get("heartbeat_timeout", 60.0)
        self.guild_ready_timeout: float = options.get("guild_ready_timeout", 2.0)
        if self.guild_ready_timeout < 0:
            raise ValueError("guild_ready_timeout cannot be negative")

        allowed_mentions = options.get("allowed_mentions")

        if allowed_mentions is not None and not isinstance(
            allowed_mentions, AllowedMentions
        ):
            raise TypeError("allowed_mentions parameter must be AllowedMentions")

        self.allowed_mentions: AllowedMentions | None = allowed_mentions
        self._chunk_requests: dict[int | str, ChunkRequest] = {}

        activity = options.get("activity", None)
        if activity:
            if not isinstance(activity, BaseActivity):
                raise TypeError("activity parameter must derive from BaseActivity.")

            activity = activity.to_dict()

        status = options.get("status", None)
        if status:
            status = "invisible" if status is Status.offline else str(status)
        intents = options.get("intents", None)
        if intents is None:
            intents = Intents.default()

        elif not isinstance(intents, Intents):
            raise TypeError(f"intents parameter must be Intent not {type(intents)!r}")
        if not intents.guilds:
            _log.warning(
                "Guilds intent seems to be disabled. This may cause state related"
                " issues."
            )

        self._chunk_guilds: bool = options.get(
            "chunk_guilds_at_startup", intents.members
        )

        # Ensure these two are set properly
        if not intents.members and self._chunk_guilds:
            raise ValueError(
                "Intents.members must be enabled to chunk guilds at startup."
            )

        cache_flags = options.get("member_cache_flags", None)
        if cache_flags is None:
            cache_flags = MemberCacheFlags.from_intents(intents)
        elif not isinstance(cache_flags, MemberCacheFlags):
            raise TypeError(
                "member_cache_flags parameter must be MemberCacheFlags not"
                f" {type(cache_flags)!r}"
            )

        else:
            cache_flags._verify_intents(intents)

        self.member_cache_flags: MemberCacheFlags = cache_flags
        self._activity: ActivityPayload | None = activity
        self._status: str | None = status
        self._intents: Intents = intents

        if not intents.members or cache_flags._empty:
            self.store_user = self.create_user  # type: ignore
            self.deref_user = self.deref_user_no_intents  # type: ignore

        self.parsers = parsers = {}
        for attr, func in inspect.getmembers(self):
            if attr.startswith("parse_"):
                parsers[attr[6:].upper()] = func

        self.clear()

    def clear(self, *, views: bool = True) -> None:
        self.user: ClientUser | None = None
        # Originally, this code used WeakValueDictionary to maintain references to the
        # global user mapping.

        # However, profiling showed that this came with two cons:

        # 1. The __weakref__ slot caused a non-trivial increase in memory
        # 2. The performance of the mapping caused store_user to be a bottleneck.

        # Since this is undesirable, a mapping is now used instead with stored
        # references now using a regular dictionary with eviction being done
        # using __del__. Testing this for memory leaks led to no discernible leaks,
        # though more testing will have to be done.
        self._users: dict[int, User] = {}
        self._emojis: dict[int, Emoji] = {}
        self._stickers: dict[int, GuildSticker] = {}
        self._guilds: dict[int, Guild] = {}
        self._polls: dict[int, Guild] = {}
        if views:
            self._view_store: ViewStore = ViewStore(self)
        self._modal_store: ModalStore = ModalStore(self)
        self._voice_clients: dict[int, VoiceClient] = {}

        # LRU of max size 128
        self._private_channels: OrderedDict[int, PrivateChannel] = OrderedDict()
        # extra dict to look up private channels by user id
        self._private_channels_by_user: dict[int, DMChannel] = {}
        if self.max_messages is not None:
            self._messages: Deque[Message] | None = deque(maxlen=self.max_messages)
        else:
            self._messages: Deque[Message] | None = None

    def process_chunk_requests(
        self, guild_id: int, nonce: str | None, members: list[Member], complete: bool
    ) -> None:
        removed = []
        for key, request in self._chunk_requests.items():
            if request.guild_id == guild_id and request.nonce == nonce:
                request.add_members(members)
                if complete:
                    request.done()
                    removed.append(key)

        for key in removed:
            del self._chunk_requests[key]

    def call_handlers(self, key: str, *args: Any, **kwargs: Any) -> None:
        try:
            func = self.handlers[key]
        except KeyError:
            pass
        else:
            func(*args, **kwargs)

    async def call_hooks(self, key: str, *args: Any, **kwargs: Any) -> None:
        try:
            coro = self.hooks[key]
        except KeyError:
            pass
        else:
            await coro(*args, **kwargs)

    @property
    def self_id(self) -> int | None:
        u = self.user
        return u.id if u else None

    @property
    def intents(self) -> Intents:
        ret = Intents.none()
        ret.value = self._intents.value
        return ret

    @property
    def voice_clients(self) -> list[VoiceClient]:
        return list(self._voice_clients.values())

    def _get_voice_client(self, guild_id: int | None) -> VoiceClient | None:
        # the keys of self._voice_clients are ints
        return self._voice_clients.get(guild_id)  # type: ignore

    def _add_voice_client(self, guild_id: int, voice: VoiceClient) -> None:
        self._voice_clients[guild_id] = voice

    def _remove_voice_client(self, guild_id: int) -> None:
        self._voice_clients.pop(guild_id, None)

    def _update_references(self, ws: DiscordWebSocket) -> None:
        for vc in self.voice_clients:
            vc.main_ws = ws  # type: ignore

    def store_user(self, data: UserPayload) -> User:
        user_id = int(data["id"])
        try:
            return self._users[user_id]
        except KeyError:
            user = User(state=self, data=data)
            if user.discriminator != "0000":
                self._users[user_id] = user
                user._stored = True
            return user

    def deref_user(self, user_id: int) -> None:
        self._users.pop(user_id, None)

    def create_user(self, data: UserPayload) -> User:
        return User(state=self, data=data)

    def deref_user_no_intents(self, user_id: int) -> None:
        return

    def get_user(self, id: int | None) -> User | None:
        # the keys of self._users are ints
        return self._users.get(id)  # type: ignore

    def store_emoji(self, guild: Guild, data: EmojiPayload) -> Emoji:
        # the id will be present here
        emoji_id = int(data["id"])  # type: ignore
        self._emojis[emoji_id] = emoji = Emoji(guild=guild, state=self, data=data)
        return emoji

    def store_sticker(self, guild: Guild, data: GuildStickerPayload) -> GuildSticker:
        sticker_id = int(data["id"])
        self._stickers[sticker_id] = sticker = GuildSticker(state=self, data=data)
        return sticker

    def store_view(self, view: View, message_id: int | None = None) -> None:
        self._view_store.add_view(view, message_id)

    def store_modal(self, modal: Modal, message_id: int) -> None:
        self._modal_store.add_modal(modal, message_id)

    def prevent_view_updates_for(self, message_id: int) -> View | None:
        return self._view_store.remove_message_tracking(message_id)

    @property
    def persistent_views(self) -> Sequence[View]:
        return self._view_store.persistent_views

    @property
    def guilds(self) -> list[Guild]:
        return list(self._guilds.values())

    def _get_guild(self, guild_id: int | None) -> Guild | None:
        # the keys of self._guilds are ints
        return self._guilds.get(guild_id)  # type: ignore

    def _add_guild(self, guild: Guild) -> None:
        self._guilds[guild.id] = guild

    def _remove_guild(self, guild: Guild) -> None:
        self._guilds.pop(guild.id, None)

        for emoji in guild.emojis:
            self._emojis.pop(emoji.id, None)

        for sticker in guild.stickers:
            self._stickers.pop(sticker.id, None)

        del guild

    @property
    def emojis(self) -> list[Emoji]:
        return list(self._emojis.values())

    @property
    def stickers(self) -> list[GuildSticker]:
        return list(self._stickers.values())

    def get_emoji(self, emoji_id: int | None) -> Emoji | None:
        # the keys of self._emojis are ints
        return self._emojis.get(emoji_id)  # type: ignore

    def get_sticker(self, sticker_id: int | None) -> GuildSticker | None:
        # the keys of self._stickers are ints
        return self._stickers.get(sticker_id)  # type: ignore

    @property
    def polls(self) -> list[Poll]:
        return list(self._polls.values())

    def store_raw_poll(self, poll: PollPayload, raw):
        channel = self.get_channel(raw.channel_id) or PartialMessageable(
            state=self, id=raw.channel_id
        )
        message = channel.get_partial_message(raw.message_id)
        p = Poll.from_dict(poll, message)
        self._polls[message.id] = p
        return p

    def store_poll(self, poll: Poll, message_id: int):
        self._polls[message_id] = poll

    def get_poll(self, message_id):
        return self._polls.get(message_id)

    @property
    def private_channels(self) -> list[PrivateChannel]:
        return list(self._private_channels.values())

    def _get_private_channel(self, channel_id: int | None) -> PrivateChannel | None:
        try:
            # the keys of self._private_channels are ints
            value = self._private_channels[channel_id]  # type: ignore
        except KeyError:
            return None
        else:
            self._private_channels.move_to_end(channel_id)  # type: ignore
            return value

    def _get_private_channel_by_user(self, user_id: int | None) -> DMChannel | None:
        # the keys of self._private_channels are ints
        return self._private_channels_by_user.get(user_id)  # type: ignore

    def _add_private_channel(self, channel: PrivateChannel) -> None:
        channel_id = channel.id
        self._private_channels[channel_id] = channel

        if len(self._private_channels) > 128:
            _, to_remove = self._private_channels.popitem(last=False)
            if isinstance(to_remove, DMChannel) and to_remove.recipient:
                self._private_channels_by_user.pop(to_remove.recipient.id, None)

        if isinstance(channel, DMChannel) and channel.recipient:
            self._private_channels_by_user[channel.recipient.id] = channel

    def add_dm_channel(self, data: DMChannelPayload) -> DMChannel:
        # self.user is *always* cached when this is called
        channel = DMChannel(me=self.user, state=self, data=data)  # type: ignore
        self._add_private_channel(channel)
        return channel

    def _remove_private_channel(self, channel: PrivateChannel) -> None:
        self._private_channels.pop(channel.id, None)
        if isinstance(channel, DMChannel):
            recipient = channel.recipient
            if recipient is not None:
                self._private_channels_by_user.pop(recipient.id, None)

    def _get_message(self, msg_id: int | None) -> Message | None:
        return (
            utils.find(lambda m: m.id == msg_id, reversed(self._messages))
            if self._messages
            else None
        )

    def _add_guild_from_data(self, data: GuildPayload) -> Guild:
        guild = Guild(data=data, state=self)
        self._add_guild(guild)
        return guild

    def _guild_needs_chunking(self, guild: Guild) -> bool:
        # If presences are enabled then we get back the old guild.large behaviour
        return (
            self._chunk_guilds
            and not guild.chunked
            and not (self._intents.presences and not guild.large)
        )

    def _get_guild_channel(
        self, data: MessagePayload, guild_id: int | None = None
    ) -> tuple[Channel | Thread, Guild | None]:
        channel_id = int(data["channel_id"])
        try:
            guild = self._get_guild(int(guild_id or data["guild_id"]))
        except KeyError:
            channel = DMChannel._from_message(self, channel_id)
            guild = None
        else:
            channel = guild and guild._resolve_channel(channel_id)

        return channel or PartialMessageable(state=self, id=channel_id), guild

    async def chunker(
        self,
        guild_id: int,
        query: str = "",
        limit: int = 0,
        presences: bool = False,
        *,
        nonce: str | None = None,
    ) -> None:
        ws = self._get_websocket(guild_id)  # This is ignored upstream
        await ws.request_chunks(
            guild_id, query=query, limit=limit, presences=presences, nonce=nonce
        )

    async def query_members(
        self,
        guild: Guild,
        query: str | None,
        limit: int,
        user_ids: list[int] | None,
        cache: bool,
        presences: bool,
    ):
        guild_id = guild.id
        ws = self._get_websocket(guild_id)
        if ws is None:
            raise RuntimeError("Somehow do not have a websocket for this guild_id")

        request = ChunkRequest(guild.id, self.loop, self._get_guild, cache=cache)
        self._chunk_requests[request.nonce] = request

        try:
            # start the query operation
            await ws.request_chunks(
                guild_id,
                query=query,
                limit=limit,
                user_ids=user_ids,
                presences=presences,
                nonce=request.nonce,
            )
            return await asyncio.wait_for(request.wait(), timeout=30.0)
        except asyncio.TimeoutError:
            _log.warning(
                (
                    "Timed out waiting for chunks with query %r and limit %d for"
                    " guild_id %d"
                ),
                query,
                limit,
                guild_id,
            )
            raise

    async def _delay_ready(self) -> None:
        try:
            states = []
            while True:
                # this snippet of code is basically waiting N seconds
                # until the last GUILD_CREATE was sent
                try:
                    guild = await asyncio.wait_for(
                        self._ready_state.get(), timeout=self.guild_ready_timeout
                    )
                except asyncio.TimeoutError:
                    break
                else:
                    if self._guild_needs_chunking(guild):
                        future = await self.chunk_guild(guild, wait=False)
                        states.append((guild, future))
                    elif guild.unavailable is False:
                        self.dispatch("guild_available", guild)
                    else:
                        self.dispatch("guild_join", guild)

            for guild, future in states:
                try:
                    await asyncio.wait_for(future, timeout=5.0)
                except asyncio.TimeoutError:
                    _log.warning(
                        "Shard ID %s timed out waiting for chunks for guild_id %s.",
                        guild.shard_id,
                        guild.id,
                    )

                if guild.unavailable is False:
                    self.dispatch("guild_available", guild)
                else:
                    self.dispatch("guild_join", guild)

            # remove the state
            try:
                del self._ready_state
            except AttributeError:
                pass  # already been deleted somehow

        except asyncio.CancelledError:
            pass
        else:
            # dispatch the event
            self.call_handlers("ready")
            self.dispatch("ready")
        finally:
            self._ready_task = None

    def parse_ready(self, data) -> None:
        if self._ready_task is not None:
            self._ready_task.cancel()

        self._ready_state = asyncio.Queue()
        self.clear(views=False)
        self.user = ClientUser(state=self, data=data["user"])
        self.store_user(data["user"])

        if self.application_id is None:
            try:
                application = data["application"]
            except KeyError:
                pass
            else:
                self.application_id = utils._get_as_snowflake(application, "id")
                # flags will always be present here
                self.application_flags = ApplicationFlags._from_value(application["flags"])  # type: ignore

        for guild_data in data["guilds"]:
            self._add_guild_from_data(guild_data)

        self.dispatch("connect")
        self._ready_task = asyncio.create_task(self._delay_ready())

    def parse_resumed(self, data) -> None:
        self.dispatch("resumed")

    def parse_application_command_permissions_update(self, data) -> None:
        # unsure what the implementation would be like
        pass

    def parse_auto_moderation_rule_create(self, data) -> None:
        rule = AutoModRule(state=self, data=data)
        self.dispatch("auto_moderation_rule_create", rule)

    def parse_auto_moderation_rule_update(self, data) -> None:
        # somehow get a 'before' object?
        rule = AutoModRule(state=self, data=data)
        self.dispatch("auto_moderation_rule_update", rule)

    def parse_auto_moderation_rule_delete(self, data) -> None:
        rule = AutoModRule(state=self, data=data)
        self.dispatch("auto_moderation_rule_delete", rule)

    def parse_auto_moderation_action_execution(self, data) -> None:
        event = AutoModActionExecutionEvent(self, data)
        self.dispatch("auto_moderation_action_execution", event)

    def parse_entitlement_create(self, data) -> None:
        event = Entitlement(data=data, state=self)
        self.dispatch("entitlement_create", event)

    def parse_entitlement_update(self, data) -> None:
        event = Entitlement(data=data, state=self)
        self.dispatch("entitlement_update", event)

    def parse_entitlement_delete(self, data) -> None:
        event = Entitlement(data=data, state=self)
        self.dispatch("entitlement_delete", event)

    def parse_message_create(self, data) -> None:
        channel, _ = self._get_guild_channel(data)
        # channel would be the correct type here
        message = Message(channel=channel, data=data, state=self)  # type: ignore
        self.dispatch("message", message)
        if self._messages is not None:
            self._messages.append(message)
        # we ensure that the channel is either a TextChannel, VoiceChannel, StageChannel, or Thread
        if channel and channel.__class__ in (
            TextChannel,
            VoiceChannel,
            StageChannel,
            Thread,
        ):
            channel.last_message_id = message.id  # type: ignore

    def parse_message_delete(self, data) -> None:
        raw = RawMessageDeleteEvent(data)
        found = self._get_message(raw.message_id)
        raw.cached_message = found
        self.dispatch("raw_message_delete", raw)
        if self._messages is not None and found is not None:
            self.dispatch("message_delete", found)
            self._messages.remove(found)

    def parse_message_delete_bulk(self, data) -> None:
        raw = RawBulkMessageDeleteEvent(data)
        if self._messages:
            found_messages = [
                message for message in self._messages if message.id in raw.message_ids
            ]
        else:
            found_messages = []
        raw.cached_messages = found_messages
        self.dispatch("raw_bulk_message_delete", raw)
        if found_messages:
            self.dispatch("bulk_message_delete", found_messages)
            for msg in found_messages:
                # self._messages won't be None here
                self._messages.remove(msg)  # type: ignore

    def parse_message_update(self, data) -> None:
        raw = RawMessageUpdateEvent(data)
        message = self._get_message(raw.message_id)
        if message is not None:
            older_message = copy.copy(message)
            raw.cached_message = older_message
            self.dispatch("raw_message_edit", raw)
            message._update(data)
            # Coerce the `after` parameter to take the new updated Member
            # ref: #5999
            older_message.author = message.author
            self.dispatch("message_edit", older_message, message)
        else:
            if poll_data := data.get("poll"):
                self.store_raw_poll(poll_data, raw)
            self.dispatch("raw_message_edit", raw)

        if "components" in data and self._view_store.is_message_tracked(raw.message_id):
            self._view_store.update_from_message(raw.message_id, data["components"])

    def parse_message_reaction_add(self, data) -> None:
        emoji = data["emoji"]
        emoji_id = utils._get_as_snowflake(emoji, "id")
        emoji = PartialEmoji.with_state(
            self, id=emoji_id, animated=emoji.get("animated", False), name=emoji["name"]
        )
        raw = RawReactionActionEvent(data, emoji, "REACTION_ADD")

        member_data = data.get("member")
        if member_data:
            guild = self._get_guild(raw.guild_id)
            if guild is not None:
                raw.member = Member(data=member_data, guild=guild, state=self)
            else:
                raw.member = None
        else:
            raw.member = None
        self.dispatch("raw_reaction_add", raw)

        # rich interface here
        message = self._get_message(raw.message_id)
        if message is not None:
            emoji = self._upgrade_partial_emoji(emoji)
            reaction = message._add_reaction(data, emoji, raw.user_id)
            user = raw.member or self._get_reaction_user(message.channel, raw.user_id)

            if user:
                self.dispatch("reaction_add", reaction, user)

    def parse_message_reaction_remove_all(self, data) -> None:
        raw = RawReactionClearEvent(data)
        self.dispatch("raw_reaction_clear", raw)

        message = self._get_message(raw.message_id)
        if message is not None:
            old_reactions = message.reactions.copy()
            message.reactions.clear()
            self.dispatch("reaction_clear", message, old_reactions)

    def parse_message_reaction_remove(self, data) -> None:
        emoji = data["emoji"]
        emoji_id = utils._get_as_snowflake(emoji, "id")
        emoji = PartialEmoji.with_state(self, id=emoji_id, name=emoji["name"])
        raw = RawReactionActionEvent(data, emoji, "REACTION_REMOVE")

        member_data = data.get("member")
        if member_data:
            guild = self._get_guild(raw.guild_id)
            if guild is not None:
                raw.member = Member(data=member_data, guild=guild, state=self)
            else:
                raw.member = None
        else:
            raw.member = None

        self.dispatch("raw_reaction_remove", raw)

        message = self._get_message(raw.message_id)
        if message is not None:
            emoji = self._upgrade_partial_emoji(emoji)
            try:
                reaction = message._remove_reaction(data, emoji, raw.user_id)
            except (AttributeError, ValueError):  # eventual consistency lol
                pass
            else:
                user = self._get_reaction_user(message.channel, raw.user_id)
                if user:
                    self.dispatch("reaction_remove", reaction, user)

    def parse_message_reaction_remove_emoji(self, data) -> None:
        emoji = data["emoji"]
        emoji_id = utils._get_as_snowflake(emoji, "id")
        emoji = PartialEmoji.with_state(self, id=emoji_id, name=emoji["name"])
        raw = RawReactionClearEmojiEvent(data, emoji)
        self.dispatch("raw_reaction_clear_emoji", raw)

        message = self._get_message(raw.message_id)
        if message is not None:
            try:
                reaction = message._clear_emoji(emoji)
            except (AttributeError, ValueError):  # eventual consistency lol
                pass
            else:
                if reaction:
                    self.dispatch("reaction_clear_emoji", reaction)

    def parse_message_poll_vote_add(self, data) -> None:
        raw = RawMessagePollVoteEvent(data, True)
        guild = self._get_guild(raw.guild_id)
        if guild:
            user = guild.get_member(raw.user_id)
        else:
            user = self.get_user(raw.user_id)
        self.dispatch("raw_poll_vote_add", raw)

        self._get_message(raw.message_id)
        poll = self.get_poll(raw.message_id)
        # if message was cached, poll has already updated but votes haven't
        if poll and poll.results:
            answer = poll.get_answer(raw.answer_id)
            counts = poll.results._answer_counts
            if answer is not None:
                if answer.id in counts:
                    counts[answer.id].count += 1
                else:
                    counts[answer.id] = PollAnswerCount(
                        {"id": answer.id, "count": 1, "me_voted": False}
                    )
        if poll is not None and user is not None:
            answer = poll.get_answer(raw.answer_id)
            if answer is not None:
                self.dispatch("poll_vote_add", poll, user, answer)

    def parse_message_poll_vote_remove(self, data) -> None:
        raw = RawMessagePollVoteEvent(data, False)
        guild = self._get_guild(raw.guild_id)
        if guild:
            user = guild.get_member(raw.user_id)
        else:
            user = self.get_user(raw.user_id)
        self.dispatch("raw_poll_vote_remove", raw)

        self._get_message(raw.message_id)
        poll = self.get_poll(raw.message_id)
        # if message was cached, poll has already updated but votes haven't
        if poll and poll.results:
            answer = poll.get_answer(raw.answer_id)
            counts = poll.results._answer_counts
            if answer is not None:
                if answer.id in counts:
                    counts[answer.id].count -= 1
        if poll is not None and user is not None:
            answer = poll.get_answer(raw.answer_id)
            if answer is not None:
                self.dispatch("poll_vote_remove", poll, user, answer)

    def parse_interaction_create(self, data) -> None:
        interaction = Interaction(data=data, state=self)
        if data["type"] == 3:  # interaction component
            custom_id = interaction.data["custom_id"]  # type: ignore
            component_type = interaction.data["component_type"]  # type: ignore
            self._view_store.dispatch(component_type, custom_id, interaction)
        if interaction.type == InteractionType.modal_submit:
            user_id, custom_id = (
                interaction.user.id,
                interaction.data["custom_id"],
            )
            asyncio.create_task(
                self._modal_store.dispatch(user_id, custom_id, interaction)
            )

        self.dispatch("interaction", interaction)

    def parse_presence_update(self, data) -> None:
        guild_id = utils._get_as_snowflake(data, "guild_id")
        # guild_id won't be None here
        guild = self._get_guild(guild_id)
        if guild is None:
            _log.debug(
                "PRESENCE_UPDATE referencing an unknown guild ID: %s. Discarding.",
                guild_id,
            )
            return

        user = data["user"]
        member_id = int(user["id"])
        member = guild.get_member(member_id)
        if member is None:
            _log.debug(
                "PRESENCE_UPDATE referencing an unknown member ID: %s. Discarding",
                member_id,
            )
            return

        old_member = Member._copy(member)
        user_update = member._presence_update(data=data, user=user)
        if user_update:
            self.dispatch("user_update", user_update[0], user_update[1])

        self.dispatch("presence_update", old_member, member)

    def parse_user_update(self, data) -> None:
        # self.user is *always* cached when this is called
        user: ClientUser = self.user  # type: ignore
        user._update(data)
        ref = self._users.get(user.id)
        if ref:
            ref._update(data)

    def parse_invite_create(self, data) -> None:
        invite = Invite.from_gateway(state=self, data=data)
        self.dispatch("invite_create", invite)

    def parse_invite_delete(self, data) -> None:
        invite = Invite.from_gateway(state=self, data=data)
        self.dispatch("invite_delete", invite)

    def parse_channel_delete(self, data) -> None:
        guild = self._get_guild(utils._get_as_snowflake(data, "guild_id"))
        channel_id = int(data["id"])
        if guild is not None:
            channel = guild.get_channel(channel_id)
            if channel is not None:
                guild._remove_channel(channel)
                self.dispatch("guild_channel_delete", channel)

    def parse_channel_update(self, data) -> None:
        channel_type = try_enum(ChannelType, data.get("type"))
        channel_id = int(data["id"])
        if channel_type is ChannelType.group:
            channel = self._get_private_channel(channel_id)
            old_channel = copy.copy(channel)
            # the channel is a GroupChannel
            channel._update_group(data)  # type: ignore
            self.dispatch("private_channel_update", old_channel, channel)
            return

        guild_id = utils._get_as_snowflake(data, "guild_id")
        guild = self._get_guild(guild_id)
        if guild is not None:
            channel = guild.get_channel(channel_id)
            if channel is not None:
                old_channel = copy.copy(channel)
                channel._update(guild, data)
                self.dispatch("guild_channel_update", old_channel, channel)
            else:
                _log.debug(
                    "CHANNEL_UPDATE referencing an unknown channel ID: %s. Discarding.",
                    channel_id,
                )
        else:
            _log.debug(
                "CHANNEL_UPDATE referencing an unknown guild ID: %s. Discarding.",
                guild_id,
            )

    def parse_channel_create(self, data) -> None:
        factory, ch_type = _channel_factory(data["type"])
        if factory is None:
            _log.debug(
                "CHANNEL_CREATE referencing an unknown channel type %s. Discarding.",
                data["type"],
            )
            return

        guild_id = utils._get_as_snowflake(data, "guild_id")
        guild = self._get_guild(guild_id)
        if guild is not None:
            # the factory can't be a DMChannel or GroupChannel here
            channel = factory(guild=guild, state=self, data=data)  # type: ignore
            guild._add_channel(channel)  # type: ignore
            self.dispatch("guild_channel_create", channel)
        else:
            _log.debug(
                "CHANNEL_CREATE referencing an unknown guild ID: %s. Discarding.",
                guild_id,
            )
            return

    def parse_channel_pins_update(self, data) -> None:
        channel_id = int(data["channel_id"])
        try:
            guild = self._get_guild(int(data["guild_id"]))
        except KeyError:
            guild = None
            channel = self._get_private_channel(channel_id)
        else:
            channel = guild and guild._resolve_channel(channel_id)

        if channel is None:
            _log.debug(
                (
                    "CHANNEL_PINS_UPDATE referencing an unknown channel ID: %s."
                    " Discarding."
                ),
                channel_id,
            )
            return

        last_pin = (
            utils.parse_time(data["last_pin_timestamp"])
            if data["last_pin_timestamp"]
            else None
        )

        if guild is None:
            self.dispatch("private_channel_pins_update", channel, last_pin)
        else:
            self.dispatch("guild_channel_pins_update", channel, last_pin)

    def parse_thread_create(self, data) -> None:
        guild_id = int(data["guild_id"])
        guild: Guild | None = self._get_guild(guild_id)
        if guild is None:
            _log.debug(
                "THREAD_CREATE referencing an unknown guild ID: %s. Discarding",
                guild_id,
            )
            return

        cached_thread = guild.get_thread(int(data["id"]))
        if not cached_thread:
            thread = Thread(guild=guild, state=guild._state, data=data)
            guild._add_thread(thread)
            if data.get("newly_created"):
                thread._add_member(
                    ThreadMember(
                        thread,
                        {
                            "id": thread.id,
                            "user_id": data["owner_id"],
                            "join_timestamp": data["thread_metadata"][
                                "create_timestamp"
                            ],
                            "flags": utils.MISSING,
                        },
                    )
                )
                self.dispatch("thread_create", thread)
        else:
            self.dispatch("thread_join", cached_thread)

    def parse_thread_update(self, data) -> None:
        guild_id = int(data["guild_id"])
        guild = self._get_guild(guild_id)
        raw = RawThreadUpdateEvent(data)
        if guild is None:
            _log.debug(
                "THREAD_UPDATE referencing an unknown guild ID: %s. Discarding",
                guild_id,
            )
            return
        else:
            thread = guild.get_thread(raw.thread_id)
            if thread is not None:
                old = copy.copy(thread)
                thread._update(data)
                if thread.archived:
                    guild._remove_thread(thread)
                self.dispatch("thread_update", old, thread)
            else:
                thread = Thread(guild=guild, state=guild._state, data=data)
                if not thread.archived:
                    guild._add_thread(thread)
                self.dispatch("thread_join", thread)
            raw.thread = thread
        self.dispatch("raw_thread_update", raw)

    def parse_thread_delete(self, data) -> None:
        guild_id = int(data["guild_id"])
        guild = self._get_guild(guild_id)

        if guild is None:
            _log.debug(
                "THREAD_DELETE referencing an unknown guild ID: %s. Discarding",
                guild_id,
            )
            return

        raw = RawThreadDeleteEvent(data)
        thread = guild.get_thread(raw.thread_id)
        raw.thread = thread

        self.dispatch("raw_thread_delete", raw)

        if thread is not None:
            guild._remove_thread(thread)  # type: ignore
            self.dispatch("thread_delete", thread)

            if (msg := thread.starting_message) is not None:
                msg.thread = None

    def parse_thread_list_sync(self, data) -> None:
        guild_id = int(data["guild_id"])
        guild: Guild | None = self._get_guild(guild_id)
        if guild is None:
            _log.debug(
                "THREAD_LIST_SYNC referencing an unknown guild ID: %s. Discarding",
                guild_id,
            )
            return

        try:
            channel_ids = set(data["channel_ids"])
        except KeyError:
            # If not provided, then the entire guild is being synced
            # So all previous thread data should be overwritten
            previous_threads = guild._threads.copy()
            guild._clear_threads()
        else:
            previous_threads = guild._filter_threads(channel_ids)

        threads = {d["id"]: guild._store_thread(d) for d in data.get("threads", [])}

        for member in data.get("members", []):
            try:
                # note: member['id'] is the thread_id
                thread = threads[member["id"]]
            except KeyError:
                continue
            else:
                thread._add_member(ThreadMember(thread, member))

        for thread in threads.values():
            old = previous_threads.pop(thread.id, None)
            if old is None:
                self.dispatch("thread_join", thread)

        for thread in previous_threads.values():
            self.dispatch("thread_remove", thread)

    def parse_thread_member_update(self, data) -> None:
        guild_id = int(data["guild_id"])
        guild: Guild | None = self._get_guild(guild_id)
        if guild is None:
            _log.debug(
                "THREAD_MEMBER_UPDATE referencing an unknown guild ID: %s. Discarding",
                guild_id,
            )
            return

        thread_id = int(data["id"])
        thread: Thread | None = guild.get_thread(thread_id)
        if thread is None:
            _log.debug(
                "THREAD_MEMBER_UPDATE referencing an unknown thread ID: %s. Discarding",
                thread_id,
            )
            return

        member = ThreadMember(thread, data)
        thread.me = member
        thread._add_member(member)

    def parse_thread_members_update(self, data) -> None:
        guild_id = int(data["guild_id"])
        guild: Guild | None = self._get_guild(guild_id)
        if guild is None:
            _log.debug(
                "THREAD_MEMBERS_UPDATE referencing an unknown guild ID: %s. Discarding",
                guild_id,
            )
            return

        thread_id = int(data["id"])
        thread: Thread | None = guild.get_thread(thread_id)
        raw = RawThreadMembersUpdateEvent(data)
        if thread is None:
            _log.debug(
                (
                    "THREAD_MEMBERS_UPDATE referencing an unknown thread ID: %s."
                    " Discarding"
                ),
                thread_id,
            )
            return

        added_members = [ThreadMember(thread, d) for d in data.get("added_members", [])]
        removed_member_ids = [int(x) for x in data.get("removed_member_ids", [])]
        self_id = self.self_id
        for member in added_members:
            thread._add_member(member)
            if member.id != self_id:
                self.dispatch("thread_member_join", member)
            else:
                thread.me = member
                self.dispatch("thread_join", thread)

        for member_id in removed_member_ids:
            member = thread._pop_member(member_id)
            if member_id != self_id:
                self.dispatch("raw_thread_member_remove", raw)
                if member is not None:
                    self.dispatch("thread_member_remove", member)
            else:
                thread.me = None
                self.dispatch("thread_remove", thread)

    def parse_guild_member_add(self, data) -> None:
        guild = self._get_guild(int(data["guild_id"]))
        if guild is None:
            _log.debug(
                "GUILD_MEMBER_ADD referencing an unknown guild ID: %s. Discarding.",
                data["guild_id"],
            )
            return

        member = Member(guild=guild, data=data, state=self)
        if self.member_cache_flags.joined:
            guild._add_member(member)

        if guild._member_count is not None:
            guild._member_count += 1

        self.dispatch("member_join", member)

    def parse_guild_member_remove(self, data) -> None:
        user = self.store_user(data["user"])
        raw = RawMemberRemoveEvent(data, user)

        guild = self._get_guild(int(data["guild_id"]))
        if guild is not None:
            if guild._member_count is not None:
                guild._member_count -= 1

            member = guild.get_member(user.id)
            if member is not None:
                raw.user = member
                guild._remove_member(member)  # type: ignore
                self.dispatch("member_remove", member)
        else:
            _log.debug(
                "GUILD_MEMBER_REMOVE referencing an unknown guild ID: %s. Discarding.",
                data["guild_id"],
            )
        self.dispatch("raw_member_remove", raw)

    def parse_guild_member_update(self, data) -> None:
        guild = self._get_guild(int(data["guild_id"]))
        user = data["user"]
        user_id = int(user["id"])
        if guild is None:
            _log.debug(
                "GUILD_MEMBER_UPDATE referencing an unknown guild ID: %s. Discarding.",
                data["guild_id"],
            )
            return

        member = guild.get_member(user_id)
        if member is not None:
            old_member = Member._copy(member)
            member._update(data)
            user_update = member._update_inner_user(user)
            if user_update:
                self.dispatch("user_update", user_update[0], user_update[1])

            self.dispatch("member_update", old_member, member)
        else:
            if self.member_cache_flags.joined:
                member = Member(data=data, guild=guild, state=self)

                # Force an update on the inner user if necessary
                user_update = member._update_inner_user(user)
                if user_update:
                    self.dispatch("user_update", user_update[0], user_update[1])

                guild._add_member(member)
            _log.debug(
                "GUILD_MEMBER_UPDATE referencing an unknown member ID: %s. Discarding.",
                user_id,
            )

    def parse_guild_emojis_update(self, data) -> None:
        guild = self._get_guild(int(data["guild_id"]))
        if guild is None:
            _log.debug(
                "GUILD_EMOJIS_UPDATE referencing an unknown guild ID: %s. Discarding.",
                data["guild_id"],
            )
            return

        before_emojis = guild.emojis
        for emoji in before_emojis:
            self._emojis.pop(emoji.id, None)
        # guild won't be None here
        guild.emojis = tuple(map(lambda d: self.store_emoji(guild, d), data["emojis"]))  # type: ignore
        self.dispatch("guild_emojis_update", guild, before_emojis, guild.emojis)

    def parse_guild_stickers_update(self, data) -> None:
        guild = self._get_guild(int(data["guild_id"]))
        if guild is None:
            _log.debug(
                (
                    "GUILD_STICKERS_UPDATE referencing an unknown guild ID: %s."
                    " Discarding."
                ),
                data["guild_id"],
            )
            return

        before_stickers = guild.stickers
        for emoji in before_stickers:
            self._stickers.pop(emoji.id, None)
        # guild won't be None here
        guild.stickers = tuple(map(lambda d: self.store_sticker(guild, d), data["stickers"]))  # type: ignore
        self.dispatch("guild_stickers_update", guild, before_stickers, guild.stickers)

    def _get_create_guild(self, data):
        if data.get("unavailable") is False:
            # GUILD_CREATE with unavailable in the response
            # usually means that the guild has become available
            # and is therefore in the cache
            guild = self._get_guild(int(data["id"]))
            if guild is not None:
                guild.unavailable = False
                guild._from_data(data)
                return guild

        return self._add_guild_from_data(data)

    def is_guild_evicted(self, guild) -> bool:
        return guild.id not in self._guilds

    async def chunk_guild(self, guild, *, wait=True, cache=None):
        # Note: This method makes an API call without timeout, and should be used in
        #       conjunction with `asyncio.wait_for(..., timeout=...)`.
        cache = cache or self.member_cache_flags.joined
        request = self._chunk_requests.get(guild.id)  # nosec B113
        if request is None:
            self._chunk_requests[guild.id] = request = ChunkRequest(
                guild.id, self.loop, self._get_guild, cache=cache
            )
            await self.chunker(guild.id, nonce=request.nonce)

        if wait:
            return await request.wait()
        return request.get_future()

    async def _chunk_and_dispatch(self, guild, unavailable):
        try:
            await asyncio.wait_for(self.chunk_guild(guild), timeout=60.0)
        except asyncio.TimeoutError:
            _log.info("Somehow timed out waiting for chunks.")

        if unavailable is False:
            self.dispatch("guild_available", guild)
        else:
            self.dispatch("guild_join", guild)

    def parse_guild_create(self, data) -> None:
        unavailable = data.get("unavailable")
        if unavailable is True:
            # joined a guild with unavailable == True so..
            return

        guild = self._get_create_guild(data)

        try:
            # Notify the on_ready state, if any, that this guild is complete.
            self._ready_state.put_nowait(guild)
        except AttributeError:
            pass
        else:
            # If we're waiting for the event, put the rest on hold
            return

        # check if it requires chunking
        if self._guild_needs_chunking(guild):
            asyncio.create_task(self._chunk_and_dispatch(guild, unavailable))
            return

        # Dispatch available if newly available
        if unavailable is False:
            self.dispatch("guild_available", guild)
        else:
            self.dispatch("guild_join", guild)

    def parse_guild_update(self, data) -> None:
        guild = self._get_guild(int(data["id"]))
        if guild is not None:
            old_guild = copy.copy(guild)
            guild._from_data(data)
            self.dispatch("guild_update", old_guild, guild)
        else:
            _log.debug(
                "GUILD_UPDATE referencing an unknown guild ID: %s. Discarding.",
                data["id"],
            )

    def parse_guild_delete(self, data) -> None:
        guild = self._get_guild(int(data["id"]))
        if guild is None:
            _log.debug(
                "GUILD_DELETE referencing an unknown guild ID: %s. Discarding.",
                data["id"],
            )
            return

        if data.get("unavailable", False):
            # GUILD_DELETE with unavailable being True means that the
            # guild that was available is now currently unavailable
            guild.unavailable = True
            self.dispatch("guild_unavailable", guild)
            return

        # do a cleanup of the messages cache
        if self._messages is not None:
            self._messages: Deque[Message] | None = deque(
                (msg for msg in self._messages if msg.guild != guild),
                maxlen=self.max_messages,
            )

        self._remove_guild(guild)
        self.dispatch("guild_remove", guild)

    def parse_guild_audit_log_entry_create(self, data) -> None:
        guild = self._get_guild(int(data["guild_id"]))
        if guild is None:
            _log.debug(
                (
                    "GUILD_AUDIT_LOG_ENTRY_CREATE referencing an unknown guild ID: %s."
                    " Discarding."
                ),
                data["guild_id"],
            )
            return
        payload = RawAuditLogEntryEvent(data)
        payload.guild = guild
        self.dispatch("raw_audit_log_entry", payload)
        user = self.get_user(payload.user_id)
        if user is not None:
            data.pop("guild_id")
            entry = AuditLogEntry(users={data["user_id"]: user}, data=data, guild=guild)
            self.dispatch("audit_log_entry", entry)

    def parse_guild_ban_add(self, data) -> None:
        # we make the assumption that GUILD_BAN_ADD is done
        # before GUILD_MEMBER_REMOVE is called
        # hence we don't remove it from cache or do anything
        # strange with it, the main purpose of this event
        # is mainly to dispatch to another event worth listening to for logging
        guild = self._get_guild(int(data["guild_id"]))
        if guild is not None:
            try:
                user = User(data=data["user"], state=self)
            except KeyError:
                pass
            else:
                member = guild.get_member(user.id) or user
                self.dispatch("member_ban", guild, member)

    def parse_guild_ban_remove(self, data) -> None:
        guild = self._get_guild(int(data["guild_id"]))
        if guild is not None and "user" in data:
            user = self.store_user(data["user"])
            self.dispatch("member_unban", guild, user)

    def parse_guild_role_create(self, data) -> None:
        guild = self._get_guild(int(data["guild_id"]))
        if guild is None:
            _log.debug(
                "GUILD_ROLE_CREATE referencing an unknown guild ID: %s. Discarding.",
                data["guild_id"],
            )
            return

        role_data = data["role"]
        role = Role(guild=guild, data=role_data, state=self)
        guild._add_role(role)
        self.dispatch("guild_role_create", role)

    def parse_guild_role_delete(self, data) -> None:
        guild = self._get_guild(int(data["guild_id"]))
        if guild is not None:
            role_id = int(data["role_id"])
            try:
                role = guild._remove_role(role_id)
            except KeyError:
                return
            else:
                self.dispatch("guild_role_delete", role)
        else:
            _log.debug(
                "GUILD_ROLE_DELETE referencing an unknown guild ID: %s. Discarding.",
                data["guild_id"],
            )

    def parse_guild_role_update(self, data) -> None:
        guild = self._get_guild(int(data["guild_id"]))
        if guild is not None:
            role_data = data["role"]
            role_id = int(role_data["id"])
            role = guild.get_role(role_id)
            if role is not None:
                old_role = copy.copy(role)
                role._update(role_data)
                self.dispatch("guild_role_update", old_role, role)
        else:
            _log.debug(
                "GUILD_ROLE_UPDATE referencing an unknown guild ID: %s. Discarding.",
                data["guild_id"],
            )

    def parse_guild_members_chunk(self, data) -> None:
        guild_id = int(data["guild_id"])
        guild = self._get_guild(guild_id)
        presences = data.get("presences", [])

        # the guild won't be None here
        members = [Member(guild=guild, data=member, state=self) for member in data.get("members", [])]  # type: ignore
        _log.debug(
            "Processed a chunk for %s members in guild ID %s.", len(members), guild_id
        )

        if presences:
            member_dict = {str(member.id): member for member in members}
            for presence in presences:
                user = presence["user"]
                member_id = user["id"]
                member = member_dict.get(member_id)
                if member is not None:
                    member._presence_update(presence, user)

        complete = data.get("chunk_index", 0) + 1 == data.get("chunk_count")
        self.process_chunk_requests(guild_id, data.get("nonce"), members, complete)

    def parse_guild_scheduled_event_create(self, data) -> None:
        guild = self._get_guild(int(data["guild_id"]))
        if guild is None:
            _log.debug(
                (
                    "GUILD_SCHEDULED_EVENT_CREATE referencing an unknown guild ID: %s."
                    " Discarding."
                ),
                data["guild_id"],
            )
            return

        creator = (
            None
            if not data.get("creator", None)
            else guild.get_member(data.get("creator_id"))
        )
        scheduled_event = ScheduledEvent(
            state=self, guild=guild, creator=creator, data=data
        )
        guild._add_scheduled_event(scheduled_event)
        self.dispatch("scheduled_event_create", scheduled_event)

    def parse_guild_scheduled_event_update(self, data) -> None:
        guild = self._get_guild(int(data["guild_id"]))
        if guild is None:
            _log.debug(
                (
                    "GUILD_SCHEDULED_EVENT_UPDATE referencing an unknown guild ID: %s."
                    " Discarding."
                ),
                data["guild_id"],
            )
            return

        creator = (
            None
            if not data.get("creator", None)
            else guild.get_member(data.get("creator_id"))
        )
        scheduled_event = ScheduledEvent(
            state=self, guild=guild, creator=creator, data=data
        )
        old_event = guild.get_scheduled_event(int(data["id"]))
        guild._add_scheduled_event(scheduled_event)
        self.dispatch("scheduled_event_update", old_event, scheduled_event)

    def parse_guild_scheduled_event_delete(self, data) -> None:
        guild = self._get_guild(int(data["guild_id"]))
        if guild is None:
            _log.debug(
                (
                    "GUILD_SCHEDULED_EVENT_DELETE referencing an unknown guild ID: %s."
                    " Discarding."
                ),
                data["guild_id"],
            )
            return

        creator = (
            None
            if not data.get("creator", None)
            else guild.get_member(data.get("creator_id"))
        )
        scheduled_event = ScheduledEvent(
            state=self, guild=guild, creator=creator, data=data
        )
        scheduled_event.status = ScheduledEventStatus.canceled
        guild._remove_scheduled_event(scheduled_event)
        self.dispatch("scheduled_event_delete", scheduled_event)

    def parse_guild_scheduled_event_user_add(self, data) -> None:
        guild = self._get_guild(int(data["guild_id"]))
        if guild is None:
            _log.debug(
                (
                    "GUILD_SCHEDULED_EVENT_USER_ADD referencing an unknown guild ID:"
                    " %s. Discarding."
                ),
                data["guild_id"],
            )
            return

        payload = RawScheduledEventSubscription(data, "USER_ADD")
        payload.guild = guild
        self.dispatch("raw_scheduled_event_user_add", payload)

        member = guild.get_member(data["user_id"])
        if member is not None:
            event = guild.get_scheduled_event(data["guild_scheduled_event_id"])
            if event:
                event.subscriber_count += 1
                guild._add_scheduled_event(event)
                self.dispatch("scheduled_event_user_add", event, member)

    def parse_guild_scheduled_event_user_remove(self, data) -> None:
        guild = self._get_guild(int(data["guild_id"]))
        if guild is None:
            _log.debug(
                (
                    "GUILD_SCHEDULED_EVENT_USER_REMOVE referencing an unknown guild ID:"
                    " %s. Discarding."
                ),
                data["guild_id"],
            )
            return

        payload = RawScheduledEventSubscription(data, "USER_REMOVE")
        payload.guild = guild
        self.dispatch("raw_scheduled_event_user_remove", payload)

        member = guild.get_member(data["user_id"])
        if member is not None:
            event = guild.get_scheduled_event(data["guild_scheduled_event_id"])
            if event:
                event.subscriber_count += 1
                guild._add_scheduled_event(event)
                self.dispatch("scheduled_event_user_remove", event, member)

    def parse_guild_integrations_update(self, data) -> None:
        guild = self._get_guild(int(data["guild_id"]))
        if guild is not None:
            self.dispatch("guild_integrations_update", guild)
        else:
            _log.debug(
                (
                    "GUILD_INTEGRATIONS_UPDATE referencing an unknown guild ID: %s."
                    " Discarding."
                ),
                data["guild_id"],
            )

    def parse_integration_create(self, data) -> None:
        guild_id = int(data.pop("guild_id"))
        guild = self._get_guild(guild_id)
        if guild is not None:
            cls, _ = _integration_factory(data["type"])
            integration = cls(data=data, guild=guild)
            self.dispatch("integration_create", integration)
        else:
            _log.debug(
                "INTEGRATION_CREATE referencing an unknown guild ID: %s. Discarding.",
                guild_id,
            )

    def parse_integration_update(self, data) -> None:
        guild_id = int(data.pop("guild_id"))
        guild = self._get_guild(guild_id)
        if guild is not None:
            cls, _ = _integration_factory(data["type"])
            integration = cls(data=data, guild=guild)
            self.dispatch("integration_update", integration)
        else:
            _log.debug(
                "INTEGRATION_UPDATE referencing an unknown guild ID: %s. Discarding.",
                guild_id,
            )

    def parse_integration_delete(self, data) -> None:
        guild_id = int(data["guild_id"])
        guild = self._get_guild(guild_id)
        if guild is not None:
            raw = RawIntegrationDeleteEvent(data)
            self.dispatch("raw_integration_delete", raw)
        else:
            _log.debug(
                "INTEGRATION_DELETE referencing an unknown guild ID: %s. Discarding.",
                guild_id,
            )

    def parse_webhooks_update(self, data) -> None:
        guild = self._get_guild(int(data["guild_id"]))
        if guild is None:
            _log.debug(
                "WEBHOOKS_UPDATE referencing an unknown guild ID: %s. Discarding",
                data["guild_id"],
            )
            return

        channel_id = data["channel_id"]
        if channel_id is not None:
            channel = guild.get_channel(int(channel_id))
            if channel is not None:
                self.dispatch("webhooks_update", channel)
            else:
                _log.debug(
                    "WEBHOOKS_UPDATE referencing an unknown channel ID: %s. Discarding.",
                    data["channel_id"],
                )
        else:
            _log.debug(
                "WEBHOOKS_UPDATE channel ID was null for guild: %s. Discarding.",
                data["guild_id"],
            )

    def parse_stage_instance_create(self, data) -> None:
        guild = self._get_guild(int(data["guild_id"]))
        if guild is not None:
            stage_instance = StageInstance(guild=guild, state=self, data=data)
            guild._stage_instances[stage_instance.id] = stage_instance
            self.dispatch("stage_instance_create", stage_instance)
        else:
            _log.debug(
                "STAGE_INSTANCE_CREATE referencing unknown guild ID: %s. Discarding.",
                data["guild_id"],
            )

    def parse_stage_instance_update(self, data) -> None:
        guild = self._get_guild(int(data["guild_id"]))
        if guild is not None:
            stage_instance = guild._stage_instances.get(int(data["id"]))
            if stage_instance is not None:
                old_stage_instance = copy.copy(stage_instance)
                stage_instance._update(data)
                self.dispatch(
                    "stage_instance_update", old_stage_instance, stage_instance
                )
            else:
                _log.debug(
                    (
                        "STAGE_INSTANCE_UPDATE referencing unknown stage instance ID:"
                        " %s. Discarding."
                    ),
                    data["id"],
                )
        else:
            _log.debug(
                "STAGE_INSTANCE_UPDATE referencing unknown guild ID: %s. Discarding.",
                data["guild_id"],
            )

    def parse_stage_instance_delete(self, data) -> None:
        guild = self._get_guild(int(data["guild_id"]))
        if guild is not None:
            try:
                stage_instance = guild._stage_instances.pop(int(data["id"]))
            except KeyError:
                pass
            else:
                self.dispatch("stage_instance_delete", stage_instance)
        else:
            _log.debug(
                "STAGE_INSTANCE_DELETE referencing unknown guild ID: %s. Discarding.",
                data["guild_id"],
            )

    def parse_voice_state_update(self, data) -> None:
        guild = self._get_guild(utils._get_as_snowflake(data, "guild_id"))
        channel_id = utils._get_as_snowflake(data, "channel_id")
        flags = self.member_cache_flags
        # self.user is *always* cached when this is called
        self_id = self.user.id  # type: ignore
        if guild is not None:
            if int(data["user_id"]) == self_id:
                voice = self._get_voice_client(guild.id)
                if voice is not None:
                    coro = voice.on_voice_state_update(data)
                    asyncio.create_task(
                        logging_coroutine(
                            coro, info="Voice Protocol voice state update handler"
                        )
                    )
            member, before, after = guild._update_voice_state(data, channel_id)  # type: ignore
            if member is not None:
                if flags.voice:
                    if (
                        channel_id is None
                        and flags._voice_only
                        and member.id != self_id
                    ):
                        # Only remove from cache if we only have the voice flag enabled
                        # Member doesn't meet the Snowflake protocol currently
                        guild._remove_member(member)  # type: ignore
                    elif channel_id is not None:
                        guild._add_member(member)

                self.dispatch("voice_state_update", member, before, after)
            else:
                _log.debug(
                    (
                        "VOICE_STATE_UPDATE referencing an unknown member ID: %s."
                        " Discarding."
                    ),
                    data["user_id"],
                )

    def parse_voice_server_update(self, data) -> None:
        try:
            key_id = int(data["guild_id"])
        except KeyError:
            key_id = int(data["channel_id"])

        vc = self._get_voice_client(key_id)
        if vc is not None:
            coro = vc.on_voice_server_update(data)
            asyncio.create_task(
                logging_coroutine(
                    coro, info="Voice Protocol voice server update handler"
                )
            )

    def parse_voice_channel_status_update(self, data) -> None:
        raw = RawVoiceChannelStatusUpdateEvent(data)
        self.dispatch("raw_voice_channel_status_update", raw)
        guild = self._get_guild(int(data["guild_id"]))
        channel_id = int(data["id"])
        if guild is not None:
            channel = guild.get_channel(channel_id)
            if channel is not None:
                old_status = channel.status
                channel.status = data.get("status", None)
                self.dispatch(
                    "voice_channel_status_update", channel, old_status, channel.status
                )
            else:
                _log.debug(
                    "VOICE_CHANNEL_STATUS_UPDATE referencing an unknown channel ID: %s. Discarding.",
                    channel_id,
                )
        else:
            _log.debug(
                "VOICE_CHANNEL_STATUS_UPDATE referencing unknown guild ID: %s. Discarding.",
                data["guild_id"],
            )

    def parse_typing_start(self, data) -> None:
        raw = RawTypingEvent(data)

        member_data = data.get("member")
        if member_data:
            guild = self._get_guild(raw.guild_id)
            if guild is not None:
                raw.member = Member(data=member_data, guild=guild, state=self)
            else:
                raw.member = None
        else:
            raw.member = None
        self.dispatch("raw_typing", raw)

        channel, guild = self._get_guild_channel(data)
        if channel is not None:
            user = raw.member or self._get_typing_user(channel, raw.user_id)

            if user is not None:
                self.dispatch("typing", channel, user, raw.when)

    def _get_typing_user(
        self, channel: MessageableChannel | None, user_id: int
    ) -> User | Member | None:
        if isinstance(channel, DMChannel):
            return channel.recipient or self.get_user(user_id)

        elif isinstance(channel, (Thread, TextChannel)) and channel.guild is not None:
            return channel.guild.get_member(user_id)  # type: ignore

        elif isinstance(channel, GroupChannel):
            return utils.find(lambda x: x.id == user_id, channel.recipients)

        return self.get_user(user_id)

    def _get_reaction_user(
        self, channel: MessageableChannel, user_id: int
    ) -> User | Member | None:
        if isinstance(channel, TextChannel):
            return channel.guild.get_member(user_id)
        return self.get_user(user_id)

    def get_reaction_emoji(self, data) -> Emoji | PartialEmoji:
        emoji_id = utils._get_as_snowflake(data, "id")

        if not emoji_id:
            return data["name"]

        try:
            return self._emojis[emoji_id]
        except KeyError:
            return PartialEmoji.with_state(
                self,
                animated=data.get("animated", False),
                id=emoji_id,
                name=data["name"],
            )

    def _upgrade_partial_emoji(self, emoji: PartialEmoji) -> Emoji | PartialEmoji | str:
        emoji_id = emoji.id
        if not emoji_id:
            return emoji.name
        try:
            return self._emojis[emoji_id]
        except KeyError:
            return emoji

    def get_channel(self, id: int | None) -> Channel | Thread | None:
        if id is None:
            return None

        pm = self._get_private_channel(id)
        if pm is not None:
            return pm

        for guild in self.guilds:
            channel = guild._resolve_channel(id)
            if channel is not None:
                return channel

    def create_message(
        self,
        *,
        channel: MessageableChannel,
        data: MessagePayload,
    ) -> Message:
        return Message(state=self, channel=channel, data=data)


class AutoShardedConnectionState(ConnectionState):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.shard_ids: list[int] | range = []
        self.shards_launched: asyncio.Event = asyncio.Event()

    def _update_message_references(self) -> None:
        # self._messages won't be None when this is called
        for msg in self._messages:  # type: ignore
            if not msg.guild:
                continue

            new_guild = self._get_guild(msg.guild.id)
            if new_guild is not None and new_guild is not msg.guild:
                channel_id = msg.channel.id
                channel = new_guild._resolve_channel(channel_id) or Object(
                    id=channel_id
                )
                # channel will either be a TextChannel, Thread or Object
                msg._rebind_cached_references(new_guild, channel)  # type: ignore

    async def chunker(
        self,
        guild_id: int,
        query: str = "",
        limit: int = 0,
        presences: bool = False,
        *,
        shard_id: int | None = None,
        nonce: str | None = None,
    ) -> None:
        ws = self._get_websocket(guild_id, shard_id=shard_id)
        await ws.request_chunks(
            guild_id, query=query, limit=limit, presences=presences, nonce=nonce
        )

    async def _delay_ready(self) -> None:
        await self.shards_launched.wait()
        processed = []
        max_concurrency = len(self.shard_ids) * 2
        current_bucket = []
        while True:
            # this snippet of code is basically waiting N seconds
            # until the last GUILD_CREATE was sent
            try:
                guild = await asyncio.wait_for(
                    self._ready_state.get(), timeout=self.guild_ready_timeout
                )
            except asyncio.TimeoutError:
                break
            else:
                if self._guild_needs_chunking(guild):
                    _log.debug(
                        (
                            "Guild ID %d requires chunking, will be done in the"
                            " background."
                        ),
                        guild.id,
                    )
                    if len(current_bucket) >= max_concurrency:
                        try:
                            await utils.sane_wait_for(
                                current_bucket, timeout=max_concurrency * 70.0
                            )
                        except asyncio.TimeoutError:
                            fmt = (
                                "Shard ID %s failed to wait for chunks from a"
                                " sub-bucket with length %d"
                            )
                            _log.warning(fmt, guild.shard_id, len(current_bucket))
                        finally:
                            current_bucket = []

                    # Chunk the guild in the background while we wait for GUILD_CREATE streaming
                    future = asyncio.ensure_future(self.chunk_guild(guild))
                    current_bucket.append(future)
                else:
                    future = self.loop.create_future()
                    future.set_result([])

                processed.append((guild, future))

        guilds = sorted(processed, key=lambda g: g[0].shard_id)
        for shard_id, info in itertools.groupby(guilds, key=lambda g: g[0].shard_id):
            children, futures = zip(*info)
            # 110 reqs/minute w/ 1 req/guild plus some buffer
            timeout = 61 * (len(children) / 110)
            try:
                await utils.sane_wait_for(futures, timeout=timeout)
            except asyncio.TimeoutError:
                _log.warning(
                    (
                        "Shard ID %s failed to wait for chunks (timeout=%.2f) for %d"
                        " guilds"
                    ),
                    shard_id,
                    timeout,
                    len(guilds),
                )
            for guild in children:
                if guild.unavailable is False:
                    self.dispatch("guild_available", guild)
                else:
                    self.dispatch("guild_join", guild)

            self.dispatch("shard_ready", shard_id)

        # remove the state
        try:
            del self._ready_state
        except AttributeError:
            pass  # already been deleted somehow

        # clear the current task
        self._ready_task = None

        # dispatch the event
        self.call_handlers("ready")
        self.dispatch("ready")

    def parse_ready(self, data) -> None:
        if not hasattr(self, "_ready_state"):
            self._ready_state = asyncio.Queue()

        self.user = user = ClientUser(state=self, data=data["user"])
        # self._users is a list of Users, we're setting a ClientUser
        self._users[user.id] = user  # type: ignore

        if self.application_id is None:
            try:
                application = data["application"]
            except KeyError:
                pass
            else:
                self.application_id = utils._get_as_snowflake(application, "id")
                self.application_flags = ApplicationFlags._from_value(
                    application["flags"]
                )

        for guild_data in data["guilds"]:
            self._add_guild_from_data(guild_data)

        if self._messages:
            self._update_message_references()

        self.dispatch("connect")
        self.dispatch("shard_connect", data["__shard_id__"])

        if self._ready_task is None:
            self._ready_task = asyncio.create_task(self._delay_ready())

    def parse_resumed(self, data) -> None:
        self.dispatch("resumed")
        self.dispatch("shard_resumed", data["__shard_id__"])
