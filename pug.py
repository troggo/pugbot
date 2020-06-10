import asyncio
from dataclasses import dataclass, replace
import os
import random
from typing import FrozenSet, List, Optional, Union

from discord import ChannelType, Member, Message, Status, TextChannel, User, errors, utils
from discord.ext import commands

MIN_PLAYERS = int(os.environ.get('MIN_PLAYERS', default='8'))
MAX_PLAYERS = 12
HOST_EMOJI = '\N{Regional Indicator Symbol Letter H}'
DONE_EMOJI = '\N{WHITE HEAVY CHECK MARK}'


pugs = {}   # per-channel pug states
locks = {}  # per-channel lock for pug states


async def update_state(msg):
    """ Update pug state using a new message. """
    async with locks[msg.channel.id]:
        # compute the next state
        next_state = await pugs[msg.channel.id].next(msg)

        if next_state.msg is None:
            # if the new state doesn't have a message, create one and add reactions
            next_state = replace(next_state, msg=await msg.channel.send(next_state))
            await asyncio.gather(*(next_state.msg.add_reaction(r) for r in next_state.REACTS))
        else:
            # otherwise just edit the existing message
            await next_state.msg.edit(content=next_state)

        print(next_state)
        pugs[msg.channel.id] = next_state


def setup(bot):

    @bot.command(aliases=['i'])
    async def init(ctx, channel: Optional[TextChannel], msg: Optional[Message]):
        """ Start the bot in a channel """
        if channel is None:
            channel = msg.channel if msg is not None else ctx.channel

        if channel.type != ChannelType.text:
            await ctx.send("I can't start a PUG in that type of channel.")
            return

        if msg is None:
            msg = await channel.send("Loading...")

        pugs[channel.id] = IdleState(bot, msg, None)
        locks[channel.id] = asyncio.Lock()
        await asyncio.gather(*(msg.add_reaction(r) for r in IdleState.REACTS))
        await update_state(msg)

    @bot.command(aliases=['p'])
    async def poke(ctx, channel: Optional[TextChannel]):
        """ Force the bot to update its state (in case it gets stuck) """
        if channel is None:
            channel = ctx.channel
        await update_state(await ctx.fetch_message(pugs[channel.id].msg.id))

    @bot.command()
    @commands.is_owner()
    async def status(ctx):
        """ Print out the bot's state in all channels. """
        await ctx.send('**Status**\n' + (
            '\n\n'.join(f"`channel: {chan_id} | msg: {state.msg.id}`\n{state}" for chan_id, state in pugs.items())
            or 'Not active in any channels.'
        ))

    @bot.command()
    @commands.is_owner()
    async def clean(ctx):
        """ Clean up the bot's messages in a channel """
        await ctx.channel.purge(check=lambda m: m.author == bot.user)

    @bot.listen()
    async def on_raw_reaction_add(*args):
        print("[event] on_raw_reaction_add")

    @bot.listen()
    async def on_raw_reaction_remove(*args):
        print("[event] on_raw_reaction_remove")

    @bot.listen()
    async def on_reaction_add(*args):
        print("[event] on_reaction_add")
        await on_reaction(*args)

    @bot.listen()
    async def on_reaction_remove(*args):
        print("[event] on_reaction_remove")
        await on_reaction(*args)

    async def on_reaction(reaction, user):
        # ignore the bot's reactions
        if user == bot.user:
            return

        # ignore reactions that aren't to the bot
        if reaction.message.author != bot.user:
            return

        if pugs[reaction.message.channel.id].msg.id == reaction.message.id:
            # update the pug state
            await update_state(reaction.message)
        else:
            # don't allow reacts to messages other than the main one
            await reaction.remove(user)


@dataclass(frozen=True)
class PugState:
    bot: commands.Bot
    msg: Optional[Message]
    notice: Optional[Message]

    @classmethod
    async def notify(cls, state, content):
        assert state.msg is not None
        if state.notice is not None:
            await state.notice.delete()
        return replace(state, notice = await state.msg.channel.send(content))


@dataclass(frozen=True)
class IdleState(PugState):
    REACTS = [HOST_EMOJI]

    hosts: FrozenSet[Union[User, Member]] = frozenset()
    players: FrozenSet[Union[User, Member]] = frozenset()

    def __str__(self):
        return (
            f"**Waiting for players**\n"
            f"React with {HOST_EMOJI} if you can host.\n"
            f"React with anything else to play.\n"
            f"```\n"
            f"{len(self.hosts)} host(s):   {strjoin(self.hosts)}\n"
            f"{len(self.players)} player(s): {strjoin(self.players)}\n"
            f"```"
        )

    async def next(self, new_msg: Message):
        assert new_msg.id == self.msg.id

        host_reacts = { r for r in new_msg.reactions if r.emoji == HOST_EMOJI }
        player_reacts = set(new_msg.reactions) - host_reacts

        hosts = frozenset([u for r in host_reacts async for u in r.users()]) - { self.bot.user }
        players = frozenset([u for r in player_reacts async for u in r.users()]) - { self.bot.user }

        # if we're still waiting for people, stay in the idle state
        if len(hosts) < 1 or len(players) < MIN_PLAYERS:
            return replace(self, msg=new_msg, hosts=hosts, players=players)

        # if there's idle players, remove them and try again
        is_afk = lambda user: isinstance(user, Member) and user.status != Status.online
        afks = list(filter(is_afk, hosts | players))
        if afks:
            next_state = await PugState.notify(self, f"Removing afk players: `{strjoin(afks)}`")
            await asyncio.gather(*(react.remove(user) for user in afks for react in new_msg.reactions))
            return await next_state.next(await new_msg.channel.fetch_message(new_msg.id))

        # start pug
        await self.msg.delete()  # delete the current message
        host = random.choice(list(hosts))
        team_size = min(MAX_PLAYERS, len(players)) // 2
        players = random.sample(players, k=team_size * 2)
        red, blu = players[:team_size], players[team_size:]
        return RunningState(self.bot, None, self.notice, host, red, blu)


@dataclass(frozen=True)
class RunningState(PugState):
    REACTS = [DONE_EMOJI]

    host: Member
    red: List[Member]
    blu: List[Member]

    def __str__(self):
        return (
            f"**PUG started**\n"
            f"React with {DONE_EMOJI} once the PUG is done.\n"
            f"```\n"
            f"Host: {self.host}\n"
            f"\n"
            f"RED: {strjoin(self.red)}\n"
            f"BLU: {strjoin(self.blu)}\n"
            f"```"
        )

    async def next(self, msg: Message):
        assert msg.id == self.msg.id

        done_react = utils.get(msg.reactions, emoji=DONE_EMOJI)
        if done_react.count > 2 or await bot_owner_reacted(self.bot, done_react):
            # update the current message
            await self.msg.clear_reactions()
            await self.msg.edit(content=(
                f"**PUG finished**\n"
                f"```\n"
                f"Host: {self.host}\n"
                f"\n"
                f"RED: {strjoin(self.red)}\n"
                f"BLU: {strjoin(self.blu)}\n"
                f"```"
            ))

            # start the next pug
            return IdleState(self.bot, msg=None, notice=self.notice)


def strjoin(it, sep=', '):
    return sep.join(map(str, it))


async def bot_owner_reacted(bot, reaction):
    """ returns whether the bot owner has reacted with 'reaction' """
    return await reaction.users().find(bot.is_owner) is not None
