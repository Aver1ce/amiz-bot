import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
import os
import json
import asyncio
import datetime
import random
import re
import requests
import typing
import zipfile
import io
import glob

# ============================================================
# SETUP
# ============================================================
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID"))
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")  # optional — leave blank in .env for free-mode-only

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.reactions = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ============================================================
# CONFIG — edit these values for your server
# ============================================================
# SLURS is pre-filled with commonly blocked slurs. Feel free to add more —
# same format, comma separated, lowercase, no need for variants (the
# normalizer below catches spaced-out/symbol-swapped versions automatically).
SLURS = [
    "nigger", "nigga", "faggot", "fag", "chink", "spic", "kike", "tranny",
    "retard", "wetback", "gook", "coon", "beaner", "raghead", "dyke",
]
# PROFANITY stays empty by default since profanity is allowed in this server.
# If you ever want to block specific swear words too, add them here the same way.
PROFANITY = []

BAD_WORDS = SLURS + PROFANITY  # what actually gets filtered — combine as you like


def normalize_text(text: str) -> str:
    """Strips spacing/symbols and un-does common letter substitutions (1->i, 3->e, etc)
    so evasion tricks like 'n1gger' or 'n i g g e r' still get caught."""
    substitutions = {"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"}
    normalized = "".join(substitutions.get(ch, ch) for ch in text.lower() if ch.isalnum() or ch in substitutions)
    return normalized


def contains_banned_word(text: str, guild_id=None) -> bool:
    normalized = normalize_text(text)
    words = BAD_WORDS
    if guild_id is not None:
        custom = guild_settings.get(str(guild_id), {}).get("banned_words", [])
        if custom:
            words = BAD_WORDS + custom
    return any(word in normalized for word in words)

SPAM_LIMIT = 5
SPAM_SECONDS = 5
TIMEOUT_ROLE_NAME = "Timed Out"
# TIMEOUT_CHANNEL_ID, WELCOME_CHANNEL_ID, GOODBYE_CHANNEL_ID, MOD_LOG_CHANNEL_ID, and
# LEVEL_UP_CHANNEL_ID used to be hardcoded here — now they're configured PER SERVER using
# Discord commands (!setwelcomechannel, !setgoodbyechannel, etc — see GUILD SETTINGS section
# below), since a single hardcoded channel ID breaks the moment the bot is in more than one
# server. No editing needed here anymore for those.

# --- Owner lock: bot only works in servers where the owner is actually a member ---
REQUIRE_OWNER_PRESENT = True     # if True, the bot goes completely inert in any server you're not in
AUTO_LEAVE_IF_NO_OWNER = True   # bot automatically leaves servers where its owner isn't present

# --- Anti-raid ---
RAID_JOIN_THRESHOLD = 6        # if this many members join within...
RAID_WINDOW_SECONDS = 15       # ...this many seconds, treat it as a raid
MIN_ACCOUNT_AGE_DAYS = 3       # accounts newer than this get auto-kicked DURING a detected raid
RAID_ACTION = "kick"           # "kick" or "ban" for new accounts caught during a raid
AUTO_LOCKDOWN_ON_RAID = True   # if True, automatically locks all channels when a raid is detected

XP_PER_MESSAGE = 15
XP_COOLDOWN_SECONDS = 60
# LEVEL_UP_CHANNEL_ID removed — level-up announcements now default to wherever the message
# was sent, or a per-server configured channel (!setlevelupchannel).

# XP per message and level-up roles are now configured PER SERVER with commands
# (!setxpamount, !setlevelrole, etc — see LEVELING CONFIG section) instead of hardcoded
# here. These two constants are only the FALLBACK used until a server sets its own.
DEFAULT_VOICE_XP_PER_MINUTE = 5  # fallback for !setvoicexpamount

# If you ever set up a website with a full leaderboard view, paste its URL here and the
# leaderboard embed will automatically add a "Click here" link to it. Leave as None until then.
LEADERBOARD_WEBSITE_URL = None  # e.g. "https://yourbot.vercel.app/leaderboard"

BOT_NAME = "Your Bot's Name"
BOT_PERSONALITY = (
    "You are a friendly, witty Discord bot with your own personality. "
    "Keep replies short and casual, like a real Discord message (1-3 sentences usually). "
    "Use a bit of playful energy and the occasional emoji, but don't overdo it."
)
AI_MODEL = "claude-sonnet-4-6"

# ============================================================
# DATA FILES (everything here persists across restarts)
# ============================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))  # anchor all data paths to the script's own folder, not whatever the current working directory happens to be when the process starts
ROLES_FILE = os.path.join(BASE_DIR, "stored_roles.json")
LEVELS_FILE = os.path.join(BASE_DIR, "levels.json")
REACTION_ROLES_FILE = os.path.join(BASE_DIR, "reaction_roles.json")
AFK_FILE = os.path.join(BASE_DIR, "afk.json")
GUILD_SETTINGS_FILE = os.path.join(BASE_DIR, "guild_settings.json")
BIRTHDAYS_FILE = os.path.join(BASE_DIR, "birthdays.json")
GIVEAWAYS_FILE = os.path.join(BASE_DIR, "giveaways.json")
STARBOARD_FILE = os.path.join(BASE_DIR, "starboard.json")


def load_json(path):
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return {}


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=4)


stored_roles = load_json(ROLES_FILE)
levels_data = load_json(LEVELS_FILE)
# reaction_roles.json format: {"message_id": {"emoji": role_id}}  (keys are always strings in JSON)
reaction_roles = load_json(REACTION_ROLES_FILE)
# afk.json format: {"user_id": {"activity": str, "since": unix_timestamp}}
afk_data = load_json(AFK_FILE)
# guild_settings.json format: {"guild_id": {"welcome_channel": id, "goodbye_channel": id,
#                                             "mod_log_channel": id, "timeout_channel": id,
#                                             "level_up_channel": id, "xp_per_message": int,
#                                             "voice_xp_per_minute": int,
#                                             "level_roles": {"15": role_id, "7": role_id},
#                                             "banned_words": [str, str, ...],
#                                             "starboard_channel": id, "starboard_threshold": int,
#                                             "birthday_channel": id}}
guild_settings = load_json(GUILD_SETTINGS_FILE)
# birthdays.json format: {"user_id": "MM-DD"}  — one birthday per user, global (not per-server)
birthdays_data = load_json(BIRTHDAYS_FILE)
# giveaways.json format: {"message_id": {"guild_id", "channel_id", "prize", "winners", "end_time"}}
giveaways_data = load_json(GIVEAWAYS_FILE)
# starboard.json format: {"original_message_id": {"guild_id", "starboard_message_id", "stars"}}
starboard_data = load_json(STARBOARD_FILE)


def get_guild_channel(guild_id, key):
    """Looks up a configured channel for a specific server (welcome_channel, mod_log_channel, etc).
    Returns the actual discord.Channel object, or None if that server hasn't set one yet."""
    settings = guild_settings.get(str(guild_id), {})
    channel_id = settings.get(key)
    return bot.get_channel(channel_id) if channel_id else None


def set_guild_channel(guild_id, key, channel_id):
    guild_settings.setdefault(str(guild_id), {})[key] = channel_id
    save_json(GUILD_SETTINGS_FILE, guild_settings)


spam_tracker = {}
xp_cooldowns = {}
voice_sessions = {}  # runtime only: "guild_id:user_id" -> last XP checkpoint timestamp


async def dm_owner(message: str, color=discord.Color.blurple()):
    try:
        owner = await bot.fetch_user(OWNER_ID)
        await owner.send(embed=discord.Embed(description=message, color=color))
    except Exception as e:
        print(f"Could not DM owner: {e}")


def owner_only():
    """Command check that only lets the bot owner (set via OWNER_ID in .env) run it —
    not server admins, not anyone else, only you specifically."""
    async def predicate(ctx):
        return ctx.author.id == OWNER_ID
    return commands.check(predicate)


def has_permissions_or_owner(**perms):
    """Drop-in replacement for the old @commands.has_permissions(...) decorator. Works exactly
    the same for everyone else, but the bot owner (OWNER_ID) always passes, regardless of their
    actual permissions in that server."""
    invalid = set(perms) - set(discord.Permissions.VALID_FLAGS)
    if invalid:
        raise TypeError(f"Invalid permission(s): {', '.join(invalid)}")

    async def predicate(ctx):
        if ctx.author.id == OWNER_ID:
            return True
        permissions = ctx.channel.permissions_for(ctx.author)
        missing = [perm for perm, value in perms.items() if getattr(permissions, perm) != value]
        if not missing:
            return True
        raise commands.MissingPermissions(missing)

    return commands.check(predicate)


def backup_permission():
    """Command check for backup/restore commands. Lets YOU (the bot owner) run these in
    ANY server, or lets a server's own owner run them for THEIR server only — a server
    owner can never touch another server's backups."""
    async def predicate(ctx):
        if ctx.author.id == OWNER_ID:
            return True
        return ctx.guild is not None and ctx.author.id == ctx.guild.owner_id
    return commands.check(predicate)


async def owner_in_guild(guild: discord.Guild) -> bool:
    """Checks whether the bot owner is actually a member of this server.
    Used to brick the bot in any server it gets added to without you in it."""
    if guild is None:
        return True  # DMs have no guild — always allow those (that's how the owner chats with her)
    member = guild.get_member(OWNER_ID)
    if member is not None:
        return True
    try:
        member = await guild.fetch_member(OWNER_ID)
        return member is not None
    except (discord.NotFound, discord.HTTPException):
        return False


async def handle_unauthorized_guild(guild: discord.Guild):
    """Called whenever the bot detects it's in a server without its owner. Logs it, DMs the owner,
    posts a heads-up in the server itself, and either leaves outright or just goes silent
    depending on AUTO_LEAVE_IF_NO_OWNER."""
    await dm_owner(f"🚨 I'm in **{guild.name}** (`{guild.id}`) but you're not a member of it — "
                    f"I'm {'leaving it' if AUTO_LEAVE_IF_NO_OWNER else 'going inert in it'} for security.")

    if AUTO_LEAVE_IF_NO_OWNER:
        # Try to say something before leaving, so the server isn't just left confused
        target_channel = guild.system_channel
        if target_channel is None or not target_channel.permissions_for(guild.me).send_messages:
            target_channel = next(
                (c for c in guild.text_channels if c.permissions_for(guild.me).send_messages), None
            )
        if target_channel:
            try:
                embed = discord.Embed(
                    title="🚪 Leaving This Server",
                    description="My owner isn't a member of this server, so I'm not authorized to operate here. Leaving now.",
                    color=discord.Color.red(),
                )
                await target_channel.send(embed=embed)
            except discord.Forbidden:
                pass
        try:
            await guild.leave()
        except Exception as e:
            await dm_owner(f"⚠️ Tried to leave {guild.name} but couldn't: {e}")


@bot.check
async def global_owner_lock(ctx):
    """Runs before EVERY command, in every server. If REQUIRE_OWNER_PRESENT is on and the owner
    isn't in this server, every command silently fails — the bot is functionally a brick here."""
    if not REQUIRE_OWNER_PRESENT:
        return True
    return await owner_in_guild(ctx.guild)


async def mod_log(guild: discord.Guild, action: str, target, moderator, reason: str = "No reason given", color=discord.Color.orange()):
    """Posts a clean embed to the mod-log channel recording who did what to whom and why.
    Also DMs the owner a short version so nothing gets missed."""
    channel = get_guild_channel(guild.id, "mod_log_channel")
    embed = discord.Embed(title=f"🛡️ {action}", color=color, timestamp=datetime.datetime.now(datetime.timezone.utc))
    embed.add_field(name="Target", value=f"{target} (`{target.id}`)", inline=True)
    embed.add_field(name="Moderator", value=f"{moderator} (`{moderator.id}`)", inline=True)
    embed.add_field(name="Reason", value=reason, inline=False)
    if channel:
        await channel.send(embed=embed)
    await dm_owner(f"🛡️ **{action}** — {target} by {moderator}. Reason: {reason}")


# ============================================================
# STARTUP
# ============================================================
@bot.event
async def setup_hook():
    """Runs exactly once, before the bot connects — the right place to start background
    loops (unlike on_ready, which can fire more than once if Discord ever reconnects)."""
    if not voice_xp_checkpoint.is_running():
        voice_xp_checkpoint.start()
    if not birthday_check.is_running():
        birthday_check.start()
    if not giveaway_check.is_running():
        giveaway_check.start()
    if not auto_data_backup.is_running():
        auto_data_backup.start()


@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} — bot is online!")
    try:
        synced = await bot.tree.sync()
        print(f"✅ Synced {len(synced)} slash commands.")
    except Exception as e:
        print(f"⚠️ Slash command sync failed: {e}")

    # Sweep every server the bot is currently in — catches cases where it was
    # added while offline, or where the owner left a server after the bot joined.
    if REQUIRE_OWNER_PRESENT:
        for guild in bot.guilds:
            if not await owner_in_guild(guild):
                await handle_unauthorized_guild(guild)

    await dm_owner(f"✅ **{bot.user.name}** just came online.")


def build_setup_guide_embed(guild: discord.Guild) -> discord.Embed:
    """The embed posted in a server right after the bot joins, explaining how to set things up."""
    embed = discord.Embed(
        title=f"👋 Thanks for adding {bot.user.name}!",
        description=(
            f"Before anything else: **make sure I stay in this server, and that I have "
            f"Administrator permission** — most of my features (backups, moderation, channel "
            f"setup) need it to work properly. My owner can run any of my commands here "
            f"regardless of their own permissions, but I'll only operate in this server at all "
            f"while they're a member of it — that's a built-in safety lock, not a bug.\n\n"
            f"Prefix commands use `!`, or use the same commands as `/slash` commands anywhere."
        ),
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="⚙️ Setup (needs Manage Server)",
        value=(
            "`setwelcomechannel` `setgoodbyechannel` `setmodlogchannel` `settimeoutchannel`\n"
            "`setlevelupchannel` `setbirthdaychannel` `setstarboardchannel` `setstarboardthreshold`\n"
            "`setxpamount` `setvoicexpamount` `setlevelrole` `addbannedword` `showsettings`"
        ),
        inline=False,
    )
    embed.add_field(
        name="🛡️ Moderation",
        value=(
            "`kick` `ban` `timeout` `untimeout` `clear` `clearuser` `clearkeyword`\n"
            "`lockdown` `unlock` `bannedwords` `removebannedword`"
        ),
        inline=False,
    )
    embed.add_field(
        name="📈 Leveling, Reaction Roles & Fun",
        value=(
            "`rank` `leaderboard` `globalleaderboard` `setlevel` `addxp` `afk`\n"
            "`giveaway` `gend` `greroll` `reactionrole` `createreactionrole` `removereactionrole`"
        ),
        inline=False,
    )
    embed.add_field(
        name="🎂 Birthdays & ⭐ Starboard",
        value="`setbirthday` `birthday` `setbirthdaychannel` `setstarboardchannel` `setstarboardthreshold`",
        inline=False,
    )
    embed.add_field(
        name="💾 Server Backups",
        value=(
            "`backupserver <name>` — saves roles/channels and keeps auto-syncing them\n"
            "`restoreserver <name> [all|roles|channels]` `listbackups` `autobackup`"
        ),
        inline=False,
    )
    embed.set_footer(text="Type !showsettings any time to see this server's current configuration.")
    return embed


async def find_greetable_channel(guild: discord.Guild):
    """Picks the best channel to post a one-time message in: the system channel if the bot
    can talk there, otherwise the first text channel it can."""
    if guild.system_channel and guild.system_channel.permissions_for(guild.me).send_messages:
        return guild.system_channel
    return next((c for c in guild.text_channels if c.permissions_for(guild.me).send_messages), None)


@bot.event
async def on_guild_join(guild):
    """Fires the instant someone adds the bot to a new server."""
    if REQUIRE_OWNER_PRESENT and not await owner_in_guild(guild):
        await handle_unauthorized_guild(guild)
        return

    await dm_owner(f"➕ Added to a new server: **{guild.name}** (`{guild.id}`).")
    channel = await find_greetable_channel(guild)
    if channel:
        try:
            await channel.send(embed=build_setup_guide_embed(guild))
        except discord.Forbidden:
            pass


def describe_error(error: Exception):
    """Maps an exception to (SHORT_CODE, plain-English message) so people see something
    understandable instead of a raw Python error. Handles the common cases explicitly;
    anything else falls back to a generic code with the raw message attached."""
    error = getattr(error, "original", error)  # unwrap discord.py's CommandInvokeError wrapper

    if isinstance(error, commands.NoPrivateMessage):
        return "SERVER_ONLY", "This command only works inside a server, not in DMs."
    if isinstance(error, commands.PrivateMessageOnly):
        return "DM_ONLY", "This command only works in DMs with me, not inside a server."
    if isinstance(error, commands.MissingPermissions):
        perms = ", ".join(error.missing_permissions)
        return "MISSING_PERMISSIONS", f"You need the **{perms}** permission to do that."
    if isinstance(error, commands.BotMissingPermissions):
        perms = ", ".join(error.missing_permissions)
        return "BOT_MISSING_PERMISSIONS", f"I need the **{perms}** permission to do that — check my role's permissions and position."
    if isinstance(error, commands.CheckFailure):
        return "NOT_ALLOWED", "You aren't allowed to run this command."
    if isinstance(error, commands.MissingRequiredArgument):
        return "MISSING_ARGUMENT", f"You're missing the `{error.param.name}` argument — check the command's usage."
    if isinstance(error, (commands.MemberNotFound, commands.UserNotFound)):
        return "NOT_FOUND", "I couldn't find that member — check the name/mention and try again."
    if isinstance(error, commands.ChannelNotFound):
        return "NOT_FOUND", "I couldn't find that channel."
    if isinstance(error, commands.RoleNotFound):
        return "NOT_FOUND", "I couldn't find that role."
    if isinstance(error, commands.BadArgument):
        return "BAD_ARGUMENT", "One of the values you gave me isn't valid — check the command's usage."
    if isinstance(error, commands.CommandOnCooldown):
        return "COOLDOWN", f"That's on cooldown — try again in {error.retry_after:.0f}s."
    if isinstance(error, discord.Forbidden):
        return "NO_PERMISSION", "I don't have permission to do that — check my role's permissions and position in the server list."
    if isinstance(error, discord.NotFound):
        return "NOT_FOUND", "Whatever I was looking for (a message, channel, member, etc.) doesn't exist anymore."
    if isinstance(error, discord.HTTPException):
        return "DISCORD_ERROR", f"Discord rejected that request: {error}"
    return "UNKNOWN_ERROR", f"Something went wrong: `{error}`"


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return  # a typo'd command name — nothing useful to say, stay quiet

    code, message = describe_error(error)
    await dm_owner(f"⚠️ [{code}] Error running `{ctx.command}` in {ctx.guild}: `{error}`")
    embed = discord.Embed(title=f"⚠️ Error — {code}", description=message, color=discord.Color.red())
    try:
        await ctx.send(embed=embed)
    except discord.Forbidden:
        pass  # can't even send the error message here — nothing more to do


# ============================================================
# WELCOME / GOODBYE
# ============================================================
@bot.event
async def on_member_join(member):
    if REQUIRE_OWNER_PRESENT and not await owner_in_guild(member.guild):
        return  # bricked — owner isn't in this server

    channel = get_guild_channel(member.guild.id, "welcome_channel")
    if channel:
        embed = discord.Embed(
            title="Welcome! 🎉",
            description=f"Hey {member.mention}, glad you're here! You're member #{member.guild.member_count}.",
            color=discord.Color.green(),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        await channel.send(embed=embed)
    await dm_owner(f"➕ **{member}** joined {member.guild.name}.")

    await check_for_raid(member)


@bot.event
async def on_member_remove(member):
    # If the person who just left IS the owner, treat this server as unauthorized from now on.
    if member.id == OWNER_ID and REQUIRE_OWNER_PRESENT:
        await handle_unauthorized_guild(member.guild)
        return

    if REQUIRE_OWNER_PRESENT and not await owner_in_guild(member.guild):
        return  # bricked — owner isn't in this server

    channel = get_guild_channel(member.guild.id, "goodbye_channel")
    if channel:
        embed = discord.Embed(
            title="Goodbye 👋",
            description=f"**{member}** has left the server.",
            color=discord.Color.red(),
        )
        await channel.send(embed=embed)
    await dm_owner(f"➖ **{member}** left {member.guild.name}.")


# ============================================================
# REACTION ROLES (now permanent — saved to reaction_roles.json) + STARBOARD
# ============================================================
STAR_EMOJI = "⭐"


async def update_starboard(payload):
    """Re-checks a message's star count against this server's threshold. Posts it to the
    starboard the first time it crosses the threshold, or just updates the star count on
    an already-posted entry. Never un-posts one that drops back below threshold."""
    guild = bot.get_guild(payload.guild_id)
    if guild is None:
        return
    settings = guild_settings.get(str(guild.id), {})
    starboard_channel_id = settings.get("starboard_channel")
    if not starboard_channel_id:
        return
    threshold = settings.get("starboard_threshold", 3)

    source_channel = guild.get_channel(payload.channel_id)
    if source_channel is None:
        return
    try:
        message = await source_channel.fetch_message(payload.message_id)
    except (discord.NotFound, discord.Forbidden):
        return

    star_reaction = discord.utils.get(message.reactions, emoji=STAR_EMOJI)
    star_count = star_reaction.count if star_reaction else 0
    entry = starboard_data.get(str(message.id))

    if entry:
        # Already posted — just keep the star count on the existing starboard message current.
        starboard_channel = guild.get_channel(entry.get("starboard_channel_id", starboard_channel_id))
        if starboard_channel is None:
            return
        try:
            starboard_message = await starboard_channel.fetch_message(entry["starboard_message_id"])
            embed = starboard_message.embeds[0]
            embed.set_footer(text=f"⭐ {star_count} stars")
            await starboard_message.edit(embed=embed)
            entry["stars"] = star_count
            save_json(STARBOARD_FILE, starboard_data)
        except (discord.NotFound, discord.Forbidden, IndexError):
            pass
        return

    if star_count < threshold:
        return  # hasn't crossed the threshold yet — nothing to post

    starboard_channel = guild.get_channel(starboard_channel_id)
    if starboard_channel is None:
        return

    embed = discord.Embed(description=message.content or "*(no text content)*", color=discord.Color.gold())
    embed.set_author(name=message.author.display_name, icon_url=message.author.display_avatar.url)
    embed.add_field(name="Source", value=f"[Jump to message]({message.jump_url})", inline=False)
    if message.attachments:
        embed.set_image(url=message.attachments[0].url)
    embed.set_footer(text=f"⭐ {star_count} stars")

    starboard_message = await starboard_channel.send(embed=embed)
    starboard_data[str(message.id)] = {
        "guild_id": guild.id, "starboard_channel_id": starboard_channel.id,
        "starboard_message_id": starboard_message.id, "stars": star_count,
    }
    save_json(STARBOARD_FILE, starboard_data)


@bot.event
async def on_raw_reaction_add(payload):
    if payload.user_id == bot.user.id:
        return

    role_map = reaction_roles.get(str(payload.message_id))
    if role_map:
        role_id = role_map.get(str(payload.emoji))
        if role_id:
            guild = bot.get_guild(payload.guild_id)
            role = guild.get_role(role_id) if guild else None
            member = guild.get_member(payload.user_id) if guild else None
            if role and member:
                await member.add_roles(role, reason="Reaction role")

    if str(payload.emoji) == STAR_EMOJI:
        await update_starboard(payload)


@bot.event
async def on_raw_reaction_remove(payload):
    role_map = reaction_roles.get(str(payload.message_id))
    if role_map:
        role_id = role_map.get(str(payload.emoji))
        if role_id:
            guild = bot.get_guild(payload.guild_id)
            role = guild.get_role(role_id) if guild else None
            member = guild.get_member(payload.user_id) if guild else None
            if role and member:
                await member.remove_roles(role, reason="Reaction role removed")

    if str(payload.emoji) == STAR_EMOJI:
        await update_starboard(payload)


@bot.hybrid_command()
@commands.guild_only()
@has_permissions_or_owner(manage_guild=True)
async def setstarboardchannel(ctx, channel: discord.TextChannel):
    """Sets THIS server's starboard channel. Usage: !setstarboardchannel #starboard"""
    set_guild_channel(ctx.guild.id, "starboard_channel", channel.id)
    await ctx.send(embed=discord.Embed(description=f"✅ Starred messages will now post to {channel.mention}.", color=discord.Color.green()))


@bot.hybrid_command()
@commands.guild_only()
@has_permissions_or_owner(manage_guild=True)
async def setstarboardthreshold(ctx, count: int):
    """Sets how many ⭐ reactions a message needs to hit the starboard in THIS server.
    Usage: !setstarboardthreshold 5 (default 3)"""
    if count < 1:
        await ctx.send(embed=discord.Embed(description="Threshold has to be at least 1.", color=discord.Color.red()))
        return
    guild_settings.setdefault(str(ctx.guild.id), {})["starboard_threshold"] = count
    save_json(GUILD_SETTINGS_FILE, guild_settings)
    await ctx.send(embed=discord.Embed(description=f"✅ Messages now need **{count}** ⭐ to hit the starboard here.", color=discord.Color.green()))


@bot.hybrid_command()
@has_permissions_or_owner(manage_roles=True)
async def reactionrole(ctx, message_id: int, emoji: str, role: discord.Role):
    """Link an emoji on a message to a role. Usage: !reactionrole <message_id> <emoji> @Role
    This is now saved permanently — it survives bot restarts."""
    reaction_roles.setdefault(str(message_id), {})[emoji] = role.id
    save_json(REACTION_ROLES_FILE, reaction_roles)
    await ctx.send(embed=discord.Embed(description=f"✅ Linked {emoji} on message `{message_id}` to **{role.name}** — saved permanently.", color=discord.Color.green()))


@bot.hybrid_command()
@has_permissions_or_owner(manage_roles=True)
async def removereactionrole(ctx, message_id: int, emoji: str):
    """Remove a reaction role pairing. Usage: !removereactionrole <message_id> <emoji>"""
    if str(message_id) in reaction_roles and emoji in reaction_roles[str(message_id)]:
        del reaction_roles[str(message_id)][emoji]
        save_json(REACTION_ROLES_FILE, reaction_roles)
        await ctx.send(embed=discord.Embed(description=f"🗑️ Removed {emoji} pairing from message `{message_id}`.", color=discord.Color.orange()))
    else:
        await ctx.send(embed=discord.Embed(description="Couldn't find that pairing.", color=discord.Color.red()))


@bot.hybrid_command()
@has_permissions_or_owner(manage_roles=True)
async def createreactionrole(ctx, emoji: str, role: discord.Role, *, label: str = None):
    """Creates a brand-new reaction role message right here in Discord — no message ID needed,
    no editing bot.py. Usage: !createreactionrole 🎮 @Gamer Get pinged for game nights
    The 'label' part is optional text describing what the role is for."""
    description = label or f"React with {emoji} to get the **{role.name}** role!"
    embed = discord.Embed(
        title="🎭 Reaction Role",
        description=description,
        color=role.color if role.color.value != 0 else discord.Color.blurple(),
    )
    embed.set_footer(text=f"React with {emoji} to get {role.name}, remove your reaction to lose it.")

    msg = await ctx.send(embed=embed)
    try:
        await msg.add_reaction(emoji)
    except discord.HTTPException:
        await ctx.send(embed=discord.Embed(
            description="⚠️ That doesn't look like a valid emoji I can react with. "
                        "The message was posted, but you'll need to add the reaction yourself, "
                        "then run `!reactionrole` with this message's ID to link it.",
            color=discord.Color.red(),
        ))
        return

    reaction_roles.setdefault(str(msg.id), {})[emoji] = role.id
    save_json(REACTION_ROLES_FILE, reaction_roles)
    await ctx.send(embed=discord.Embed(description=f"✅ Done! That message above is now live — react to it and you'll get **{role.name}**.", color=discord.Color.green()), delete_after=8)


@bot.hybrid_command()
@commands.guild_only()
@has_permissions_or_owner(manage_guild=True)
async def setavatar(ctx):
    """Sets a DIFFERENT bot avatar/pfp just for this server (Discord supports per-server bot avatars).
    Attach an image to this command's message. Needs Manage Server permission."""
    if not ctx.message.attachments:
        await ctx.send(embed=discord.Embed(description="Attach an image with this command to set a per-server avatar.", color=discord.Color.red()))
        return
    image_bytes = await ctx.message.attachments[0].read()
    try:
        await ctx.guild.me.edit(avatar=image_bytes)
        await ctx.send(embed=discord.Embed(description="✅ New avatar set for this server only — my global avatar elsewhere is unchanged.", color=discord.Color.green()))
    except discord.HTTPException as e:
        await ctx.send(embed=discord.Embed(description=f"⚠️ Couldn't set that avatar: {e}", color=discord.Color.red()))


@bot.hybrid_command()
@commands.guild_only()
@has_permissions_or_owner(manage_guild=True)
async def setnickname(ctx, *, nickname: str):
    """Sets the bot's nickname for THIS server only. Needs Manage Server permission.
    Usage: !setnickname Amiz"""
    await ctx.guild.me.edit(nick=nickname)
    await ctx.send(embed=discord.Embed(description=f"✅ Nickname set to **{nickname}** for this server.", color=discord.Color.green()))


# ============================================================
# GUILD SETTINGS — every server the bot is in configures its OWN channels for
# welcome/goodbye/mod-log/timeout/level-up. Needs Manage Server permission to set.
# ============================================================
@bot.hybrid_command()
@commands.guild_only()
@has_permissions_or_owner(manage_guild=True)
async def setwelcomechannel(ctx, channel: discord.TextChannel):
    """Sets THIS server's welcome message channel. Usage: !setwelcomechannel #welcome"""
    set_guild_channel(ctx.guild.id, "welcome_channel", channel.id)
    await ctx.send(embed=discord.Embed(description=f"✅ Welcome messages will now post in {channel.mention}.", color=discord.Color.green()))


@bot.hybrid_command()
@commands.guild_only()
@has_permissions_or_owner(manage_guild=True)
async def setgoodbyechannel(ctx, channel: discord.TextChannel):
    """Sets THIS server's goodbye message channel. Usage: !setgoodbyechannel #goodbye"""
    set_guild_channel(ctx.guild.id, "goodbye_channel", channel.id)
    await ctx.send(embed=discord.Embed(description=f"✅ Goodbye messages will now post in {channel.mention}.", color=discord.Color.green()))


@bot.hybrid_command()
@commands.guild_only()
@has_permissions_or_owner(manage_guild=True)
async def setmodlogchannel(ctx, channel: discord.TextChannel):
    """Sets THIS server's mod-log channel. Usage: !setmodlogchannel #mod-log"""
    set_guild_channel(ctx.guild.id, "mod_log_channel", channel.id)
    await ctx.send(embed=discord.Embed(description=f"✅ Mod actions will now log to {channel.mention}.", color=discord.Color.green()))


@bot.hybrid_command()
@commands.guild_only()
@has_permissions_or_owner(manage_guild=True)
async def settimeoutchannel(ctx, channel: discord.TextChannel):
    """Sets THIS server's timeout channel (visible to timed-out members, they can't talk in it).
    Usage: !settimeoutchannel #timeout"""
    set_guild_channel(ctx.guild.id, "timeout_channel", channel.id)
    await ctx.send(embed=discord.Embed(description=f"✅ Timed-out members will now only see {channel.mention}.", color=discord.Color.green()))


@bot.hybrid_command()
@commands.guild_only()
@has_permissions_or_owner(manage_guild=True)
async def setlevelupchannel(ctx, channel: discord.TextChannel):
    """Sets THIS server's level-up announcement channel. Usage: !setlevelupchannel #levels
    If never set, level-up messages just post in whichever channel the person was chatting in."""
    set_guild_channel(ctx.guild.id, "level_up_channel", channel.id)
    await ctx.send(embed=discord.Embed(description=f"✅ Level-up announcements will now post in {channel.mention}.", color=discord.Color.green()))


# ============================================================
# PER-SERVER BANNED WORDS — on top of the built-in slur filter, each server can add
# its OWN extra words to block. Needs Manage Server permission.
# ============================================================
@bot.hybrid_command()
@commands.guild_only()
@has_permissions_or_owner(manage_guild=True)
async def addbannedword(ctx, *, word: str):
    """Adds a word to THIS server's custom banned-word list (on top of the built-in filter).
    Usage: !addbannedword sometermsused"""
    word = word.lower().strip()
    settings = guild_settings.setdefault(str(ctx.guild.id), {})
    words = settings.setdefault("banned_words", [])
    if word in words:
        await ctx.send(embed=discord.Embed(description="That word is already banned here.", color=discord.Color.greyple()))
        return
    words.append(word)
    save_json(GUILD_SETTINGS_FILE, guild_settings)
    await ctx.send(embed=discord.Embed(description=f"✅ Added to this server's banned words list. ({len(words)} custom word(s) total)", color=discord.Color.green()))


@bot.hybrid_command()
@commands.guild_only()
@has_permissions_or_owner(manage_guild=True)
async def removebannedword(ctx, *, word: str):
    """Removes a word from THIS server's custom banned-word list. Usage: !removebannedword sometermsused"""
    word = word.lower().strip()
    words = guild_settings.get(str(ctx.guild.id), {}).get("banned_words", [])
    if word not in words:
        await ctx.send(embed=discord.Embed(description="That word isn't on this server's custom list.", color=discord.Color.red()))
        return
    words.remove(word)
    save_json(GUILD_SETTINGS_FILE, guild_settings)
    await ctx.send(embed=discord.Embed(description=f"🗑️ Removed. ({len(words)} custom word(s) left)", color=discord.Color.orange()))


@bot.hybrid_command()
@commands.guild_only()
@has_permissions_or_owner(manage_guild=True)
async def bannedwords(ctx):
    """Lists THIS server's custom banned words (not the built-in base filter)."""
    words = guild_settings.get(str(ctx.guild.id), {}).get("banned_words", [])
    if not words:
        await ctx.send(embed=discord.Embed(description="No custom banned words added yet — use `!addbannedword`.", color=discord.Color.greyple()))
        return
    await ctx.send(embed=discord.Embed(title="🚫 Custom Banned Words", description=", ".join(f"`{w}`" for w in words), color=discord.Color.blurple()))


# ============================================================
# LEVELING CONFIG — XP-per-message, voice XP rate, and level-up role rewards, all
# configured PER SERVER. Needs Manage Server permission.
# ============================================================
@bot.hybrid_command()
@commands.guild_only()
@has_permissions_or_owner(manage_guild=True)
async def setxpamount(ctx, amount: int):
    """Sets how much XP a message earns in THIS server. Usage: !setxpamount 20 (default 15)"""
    if amount < 1:
        await ctx.send(embed=discord.Embed(description="XP amount has to be at least 1.", color=discord.Color.red()))
        return
    guild_settings.setdefault(str(ctx.guild.id), {})["xp_per_message"] = amount
    save_json(GUILD_SETTINGS_FILE, guild_settings)
    await ctx.send(embed=discord.Embed(description=f"✅ Messages now earn **{amount} XP** in this server.", color=discord.Color.green()))


@bot.hybrid_command()
@commands.guild_only()
@has_permissions_or_owner(manage_guild=True)
async def setvoicexpamount(ctx, amount: int):
    """Sets how much XP per minute members earn for being active in a voice channel in
    THIS server. Usage: !setvoicexpamount 5 (default 5). Set to 0 to disable voice XP here."""
    if amount < 0:
        await ctx.send(embed=discord.Embed(description="XP amount can't be negative.", color=discord.Color.red()))
        return
    guild_settings.setdefault(str(ctx.guild.id), {})["voice_xp_per_minute"] = amount
    save_json(GUILD_SETTINGS_FILE, guild_settings)
    await ctx.send(embed=discord.Embed(description=f"✅ Voice XP set to **{amount} XP/minute** in this server.", color=discord.Color.green()))


@bot.hybrid_command()
@commands.guild_only()
@has_permissions_or_owner(manage_guild=True)
@discord.app_commands.describe(state="Turn the XP/leveling system on or off for this server")
async def togglelevels(ctx, state: typing.Literal["on", "off"]):
    """Turns the whole XP/leveling system on or off for THIS server (chat XP, voice XP,
    level-ups, and level-role rewards all stop when it's off). Usable by you (the bot owner)
    or this server's own owner/Manage Server holders. Usage: !togglelevels off"""
    guild_settings.setdefault(str(ctx.guild.id), {})["leveling_enabled"] = (state == "on")
    save_json(GUILD_SETTINGS_FILE, guild_settings)
    if state == "on":
        await ctx.send(embed=discord.Embed(description="✅ Leveling is now **on** for this server.", color=discord.Color.green()))
    else:
        await ctx.send(embed=discord.Embed(description="🛑 Leveling is now **off** for this server — no more XP, level-ups, or level-role rewards until it's turned back on.", color=discord.Color.orange()))


@bot.hybrid_command()
@commands.guild_only()
@has_permissions_or_owner(manage_guild=True)
async def setlevelrole(ctx, level: int, role: discord.Role):
    """Sets a role to be auto-given when someone reaches a level, IN THIS SERVER ONLY.
    Usage: !setlevelrole 15 @Wizard — every server can use completely different levels/roles."""
    settings = guild_settings.setdefault(str(ctx.guild.id), {})
    level_roles = settings.setdefault("level_roles", {})
    level_roles[str(level)] = role.id
    save_json(GUILD_SETTINGS_FILE, guild_settings)
    await ctx.send(embed=discord.Embed(description=f"✅ Reaching **Level {level}** now grants {role.mention} in this server — just like Arcane's level-role rewards. Members keep every role they've earned as they level up further.", color=discord.Color.green()))


@bot.hybrid_command()
@commands.guild_only()
@has_permissions_or_owner(manage_guild=True)
async def removelevelrole(ctx, level: int):
    """Removes a level-up role reward from THIS server. Usage: !removelevelrole 15"""
    level_roles = guild_settings.get(str(ctx.guild.id), {}).get("level_roles", {})
    if str(level) not in level_roles:
        await ctx.send(embed=discord.Embed(description=f"No role is set for level {level} here.", color=discord.Color.red()))
        return
    del level_roles[str(level)]
    save_json(GUILD_SETTINGS_FILE, guild_settings)
    await ctx.send(embed=discord.Embed(description=f"🗑️ Removed the level {level} role reward.", color=discord.Color.orange()))


@bot.hybrid_command()
@commands.guild_only()
async def listlevelroles(ctx):
    """Shows every level-up role reward configured for THIS server."""
    level_roles = guild_settings.get(str(ctx.guild.id), {}).get("level_roles", {})
    if not level_roles:
        await ctx.send(embed=discord.Embed(description="No level-up roles configured here yet.", color=discord.Color.greyple()))
        return
    lines = []
    for level_str, role_id in sorted(level_roles.items(), key=lambda x: int(x[0])):
        role = ctx.guild.get_role(role_id)
        lines.append(f"**Level {level_str}** → {role.mention if role else '`(deleted role)`'}")
    await ctx.send(embed=discord.Embed(title="🏅 Level-Up Roles", description="\n".join(lines), color=discord.Color.gold()))


@bot.hybrid_command()
@commands.guild_only()
@has_permissions_or_owner(manage_guild=True)
async def setlevel(ctx, member: discord.Member, level: int):
    """Directly sets a member's level in THIS server (resets their XP progress to 0 for that
    level). Usage: !setlevel @someone 10"""
    if level < 0:
        await ctx.send(embed=discord.Embed(description="Level can't be negative.", color=discord.Color.red()))
        return
    guild_id = str(ctx.guild.id)
    guild_levels = levels_data.setdefault(guild_id, {})
    guild_levels[str(member.id)] = {"xp": 0, "level": level}
    save_json(LEVELS_FILE, levels_data)
    await ctx.send(embed=discord.Embed(description=f"✅ Set {member.mention}'s level to **{level}** in this server.", color=discord.Color.green()))


@bot.hybrid_command()
@commands.guild_only()
@has_permissions_or_owner(manage_guild=True)
async def addxp(ctx, member: discord.Member, amount: int):
    """Gives a member a specific amount of XP in THIS server (handles level-ups + role
    rewards the same as normal chat XP). Usage: !addxp @someone 500. Use a negative
    number to take XP away."""
    await grant_xp(member, ctx.guild, amount, announce_channel=ctx.channel)
    data = levels_data.get(str(ctx.guild.id), {}).get(str(member.id), {"xp": 0, "level": 0})
    await ctx.send(embed=discord.Embed(description=f"✅ Gave {member.mention} **{amount} XP**. Now: Level {data['level']}, {data['xp']} XP.", color=discord.Color.green()))


@bot.hybrid_command()
@commands.guild_only()
async def showsettings(ctx):
    """Shows this server's currently configured channels and settings."""
    settings = guild_settings.get(str(ctx.guild.id), {})
    embed = discord.Embed(title=f"⚙️ Settings for {ctx.guild.name}", color=discord.Color.blurple())
    for label, key in [("Welcome channel", "welcome_channel"), ("Goodbye channel", "goodbye_channel"),
                        ("Mod-log channel", "mod_log_channel"), ("Timeout channel", "timeout_channel"),
                        ("Level-up channel", "level_up_channel"), ("Birthday channel", "birthday_channel"),
                        ("Starboard channel", "starboard_channel")]:
        channel_id = settings.get(key)
        value = f"<#{channel_id}>" if channel_id else "*not set*"
        embed.add_field(name=label, value=value, inline=False)

    embed.add_field(name="XP per message", value=str(settings.get("xp_per_message", XP_PER_MESSAGE)), inline=True)
    embed.add_field(name="Voice XP/minute", value=str(settings.get("voice_xp_per_minute", DEFAULT_VOICE_XP_PER_MINUTE)), inline=True)
    embed.add_field(name="Starboard threshold", value=str(settings.get("starboard_threshold", 3)), inline=True)
    embed.add_field(name="Custom banned words", value=str(len(settings.get("banned_words", []))), inline=True)
    embed.add_field(name="Level-up roles set", value=str(len(settings.get("level_roles", {}))), inline=True)
    await ctx.send(embed=embed)


# ============================================================
# MOD LOGGING — catches actions done through Discord's own UI too,
# not just the bot's commands, by reading the server's audit log.
# ============================================================
async def get_audit_actor(guild: discord.Guild, action: discord.AuditLogAction, target_id: int):
    """Looks up who performed a recent action on a specific target via the audit log.
    Requires the bot to have 'View Audit Log' permission."""
    try:
        async for entry in guild.audit_logs(limit=5, action=action):
            if entry.target and entry.target.id == target_id:
                # only trust entries from the last ~10 seconds so we don't misattribute old actions
                age = (datetime.datetime.now(datetime.timezone.utc) - entry.created_at).total_seconds()
                if age < 10:
                    return entry.user, entry.reason or "No reason given"
    except discord.Forbidden:
        pass
    return None, None


@bot.event
async def on_member_ban(guild, user):
    moderator, reason = await get_audit_actor(guild, discord.AuditLogAction.ban, user.id)
    await mod_log(guild, "Member Banned", user, moderator or "Unknown", reason or "No reason given", discord.Color.red())


@bot.event
async def on_member_unban(guild, user):
    moderator, reason = await get_audit_actor(guild, discord.AuditLogAction.unban, user.id)
    await mod_log(guild, "Member Unbanned", user, moderator or "Unknown", reason or "No reason given", discord.Color.green())


@bot.event
async def on_guild_channel_create(channel):
    """Makes sure new channels automatically get hidden from timed-out members too,
    so the Timed Out role doesn't need manual re-setup every time a channel is added."""
    timeout_role = discord.utils.get(channel.guild.roles, name=TIMEOUT_ROLE_NAME)
    if timeout_role is None:
        return
    timeout_channel = get_guild_channel(channel.guild.id, "timeout_channel")
    try:
        if timeout_channel and channel.id == timeout_channel.id:
            await channel.set_permissions(timeout_role, view_channel=True, send_messages=False, speak=False, add_reactions=False)
        else:
            await channel.set_permissions(timeout_role, view_channel=False, send_messages=False, speak=False)
    except discord.Forbidden:
        pass
    schedule_auto_backup(channel.guild)


guilds_in_bulk_delete = set()  # guild IDs currently undergoing an !anni* wipe — suppresses the
                                # normal per-channel/per-role mod-log DM so a 40-channel wipe
                                # doesn't send 40 separate DMs; the anni command sends its own
                                # single summary instead.


@bot.event
async def on_guild_channel_delete(channel):
    schedule_auto_backup(channel.guild)
    if channel.guild.id in guilds_in_bulk_delete:
        return
    moderator, reason = await get_audit_actor(channel.guild, discord.AuditLogAction.channel_delete, channel.id)
    await mod_log(channel.guild, "Channel Deleted", channel, moderator or "Unknown", f"#{channel.name} — {reason or 'No reason given'}", discord.Color.dark_red())


@bot.event
async def on_guild_channel_update(before, after):
    schedule_auto_backup(after.guild)


@bot.event
async def on_guild_role_create(role):
    schedule_auto_backup(role.guild)


@bot.event
async def on_guild_role_delete(role):
    schedule_auto_backup(role.guild)
    if role.guild.id in guilds_in_bulk_delete:
        return
    moderator, reason = await get_audit_actor(role.guild, discord.AuditLogAction.role_delete, role.id)
    await mod_log(role.guild, "Role Deleted", role, moderator or "Unknown", f"@{role.name} — {reason or 'No reason given'}", discord.Color.dark_red())


@bot.event
async def on_guild_role_update(before, after):
    schedule_auto_backup(after.guild)


@bot.event
async def on_member_update(before, after):
    # Only re-sync when the change is actually roles — this event also fires for nickname
    # changes, avatar changes, etc, and those don't affect the backup at all.
    if before.roles != after.roles:
        schedule_auto_backup(after.guild)


# ============================================================
# ANTI-RAID
# ============================================================
recent_joins = []          # list of join timestamps, used to detect a burst of joins
raid_mode_active = False   # True while the server is in lockdown from a detected raid


async def lock_all_channels(guild: discord.Guild):
    """Sets @everyone's send_messages permission to False in every text channel."""
    for channel in guild.text_channels:
        try:
            overwrite = channel.overwrites_for(guild.default_role)
            overwrite.send_messages = False
            await channel.set_permissions(guild.default_role, overwrite=overwrite, reason="Anti-raid lockdown")
        except discord.Forbidden:
            pass


async def unlock_all_channels(guild: discord.Guild):
    """Restores @everyone's send_messages permission to default (unset) in every text channel."""
    for channel in guild.text_channels:
        try:
            overwrite = channel.overwrites_for(guild.default_role)
            overwrite.send_messages = None
            await channel.set_permissions(guild.default_role, overwrite=overwrite, reason="Anti-raid lockdown lifted")
        except discord.Forbidden:
            pass


async def check_for_raid(member: discord.Member):
    global raid_mode_active
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()

    recent_joins.append(now)
    while recent_joins and now - recent_joins[0] > RAID_WINDOW_SECONDS:
        recent_joins.pop(0)

    # --- Trigger raid mode if too many joins happened too fast ---
    if len(recent_joins) >= RAID_JOIN_THRESHOLD and not raid_mode_active:
        raid_mode_active = True
        await mod_log(member.guild, "🚨 RAID DETECTED", member.guild.me, bot.user,
                       f"{len(recent_joins)} joins within {RAID_WINDOW_SECONDS}s. Auto-lockdown: {AUTO_LOCKDOWN_ON_RAID}",
                       discord.Color.dark_red())
        if AUTO_LOCKDOWN_ON_RAID:
            await lock_all_channels(member.guild)

    # --- While raid mode is active, auto-kick/ban new accounts joining ---
    if raid_mode_active:
        account_age_days = (datetime.datetime.now(datetime.timezone.utc) - member.created_at).days
        if account_age_days < MIN_ACCOUNT_AGE_DAYS:
            reason = f"Anti-raid: account is only {account_age_days} day(s) old, joined during active raid"
            try:
                if RAID_ACTION == "ban":
                    await member.ban(reason=reason)
                else:
                    await member.kick(reason=reason)
                await mod_log(member.guild, f"Anti-Raid Auto-{RAID_ACTION.title()}", member, bot.user, reason, discord.Color.dark_red())
            except discord.Forbidden:
                await dm_owner(f"⚠️ Tried to auto-{RAID_ACTION} {member} during a raid but didn't have permission.")


@bot.hybrid_command()
@commands.guild_only()
@has_permissions_or_owner(administrator=True)
async def lockdown(ctx):
    """Manually locks all text channels (stops @everyone from sending messages)."""
    global raid_mode_active
    raid_mode_active = True
    await lock_all_channels(ctx.guild)
    await ctx.send(embed=discord.Embed(description="🔒 Server locked down. Use `!unlock` when it's safe.", color=discord.Color.dark_red()))
    await mod_log(ctx.guild, "Manual Lockdown", ctx.guild.me, ctx.author, "Manually triggered", discord.Color.dark_red())


@bot.hybrid_command()
@commands.guild_only()
@has_permissions_or_owner(administrator=True)
async def unlock(ctx):
    """Manually lifts a lockdown and turns off raid mode."""
    global raid_mode_active
    raid_mode_active = False
    recent_joins.clear()
    await unlock_all_channels(ctx.guild)
    await ctx.send(embed=discord.Embed(description="🔓 Lockdown lifted. Server's back to normal.", color=discord.Color.green()))
    await mod_log(ctx.guild, "Lockdown Lifted", ctx.guild.me, ctx.author, "Manually lifted", discord.Color.green())


# ============================================================
# AFK SYSTEM
# ============================================================
def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)

    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if not parts:
        parts.append(f"{seconds}s")
    return " ".join(parts)


# ============================================================
# AFK SYSTEM — one global AFK status per user (not per-server): going AFK in one server
# shows you as AFK everywhere the bot can see you. Each server gets its own locked-down
# "afk-list" channel (auto-created the first time it's needed) that always shows exactly
# who's currently away, updated live whenever anyone's status changes anywhere.
# ============================================================
AFK_CHANNEL_NAME = "afk-list"


async def get_or_create_afk_channel(guild: discord.Guild):
    """Returns this server's AFK-list channel, auto-creating it (locked so regular members
    can view but not post in it) the first time it's needed."""
    settings = guild_settings.setdefault(str(guild.id), {})
    channel = guild.get_channel(settings.get("afk_channel_id")) if settings.get("afk_channel_id") else None
    if channel is not None:
        return channel

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=True, send_messages=False, add_reactions=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_messages=True),
    }
    try:
        channel = await guild.create_text_channel(AFK_CHANNEL_NAME, overwrites=overwrites, reason="Auto-created AFK list channel")
    except discord.Forbidden:
        return None
    settings["afk_channel_id"] = channel.id
    save_json(GUILD_SETTINGS_FILE, guild_settings)
    return channel


async def refresh_afk_channel(guild: discord.Guild):
    """Rebuilds/edits the standing 'who's AFK right now' message in this server's AFK-list
    channel, based on the current GLOBAL afk_data."""
    channel = await get_or_create_afk_channel(guild)
    if channel is None:
        return

    lines = []
    for user_id_str, data in afk_data.items():
        member = guild.get_member(int(user_id_str))
        if member is None:
            continue
        duration = format_duration(datetime.datetime.now(datetime.timezone.utc).timestamp() - data["since"])
        lines.append(f"🌙 {member.mention} — *{data['activity']}* (away {duration})")

    embed = discord.Embed(
        title="🌙 Currently AFK",
        description="\n".join(lines) if lines else "Nobody is AFK right now.",
        color=discord.Color.blurple(),
    )
    embed.set_footer(text="Updates automatically whenever someone goes AFK or comes back — anywhere the bot can see them.")

    settings = guild_settings.setdefault(str(guild.id), {})
    message = None
    if settings.get("afk_list_message_id"):
        try:
            message = await channel.fetch_message(settings["afk_list_message_id"])
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            message = None

    try:
        if message:
            await message.edit(embed=embed)
        else:
            new_message = await channel.send(embed=embed)
            settings["afk_list_message_id"] = new_message.id
            save_json(GUILD_SETTINGS_FILE, guild_settings)
    except discord.Forbidden:
        pass


async def sync_afk_status_for_member(user_id: int):
    """Call this whenever one member's global AFK status changes. Refreshes the AFK-list
    channel in every server the bot shares with them — so a status set in one server shows
    up correctly in all the others too."""
    for guild in bot.guilds:
        if guild.get_member(user_id):
            await refresh_afk_channel(guild)


@bot.hybrid_command()
async def afk(ctx, *, activity: str = "AFK"):
    """Marks you as AFK — GLOBALLY, across every server the bot shares with you, not just
    this one. Usage: !afk sleeping
    Anyone who pings or replies to you (in any of those servers) will be told you're away,
    what you're doing, and how long you've been gone. Also shows up in each server's AFK-list
    channel. Clears automatically the next time you send a message anywhere."""
    afk_data[str(ctx.author.id)] = {"activity": activity, "since": datetime.datetime.now(datetime.timezone.utc).timestamp()}
    save_json(AFK_FILE, afk_data)
    embed = discord.Embed(description=f"**{activity}**", color=discord.Color.greyple())
    embed.set_author(name=f"{ctx.author.display_name} is now AFK", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)
    await sync_afk_status_for_member(ctx.author.id)


async def handle_afk(message: discord.Message):
    """Called on every message: clears the sender's own AFK status if they had one,
    and warns anyone who pinged/replied to a currently-AFK member."""
    user_id = str(message.author.id)

    if user_id in afk_data:
        since = afk_data[user_id]["since"]
        del afk_data[user_id]
        save_json(AFK_FILE, afk_data)
        duration = format_duration(datetime.datetime.now(datetime.timezone.utc).timestamp() - since)
        welcome_embed = discord.Embed(description=f"Welcome back — you were away for **{duration}**.", color=discord.Color.green())
        welcome_embed.set_author(name=message.author.display_name, icon_url=message.author.display_avatar.url)
        await message.channel.send(embed=welcome_embed, delete_after=8)
        await sync_afk_status_for_member(message.author.id)

    # Figure out who's being pinged or replied to in this message
    targets = list(message.mentions)
    if message.reference and message.reference.resolved:
        ref_author = getattr(message.reference.resolved, "author", None)
        if ref_author and ref_author not in targets:
            targets.append(ref_author)

    already_notified = set()
    for target in targets:
        if target.id == message.author.id or target.id in already_notified:
            continue
        target_data = afk_data.get(str(target.id))
        if target_data:
            duration = format_duration(datetime.datetime.now(datetime.timezone.utc).timestamp() - target_data["since"])
            embed = discord.Embed(description=f"**{target_data['activity']}** — away for {duration}", color=discord.Color.greyple())
            embed.set_author(name=f"{target.display_name} is AFK", icon_url=target.display_avatar.url)
            await message.channel.send(embed=embed)
            already_notified.add(target.id)


# ============================================================
# LEVELING (per-server — each server has its own XP/levels)
# levels_data format: {"guild_id": {"user_id": {"xp": int, "level": int}}}
# ============================================================
def get_level_xp(level):
    return 5 * (level ** 2) + 50 * level + 100


def total_xp_for(level, xp):
    """Converts a level+xp pair into one cumulative XP number (used for the global leaderboard)."""
    total = sum(get_level_xp(lvl) for lvl in range(level))
    return total + xp


def level_from_total(total_xp):
    """Reverses total_xp_for — turns a cumulative XP number back into a level + remaining xp."""
    level = 0
    remaining = total_xp
    while remaining >= get_level_xp(level):
        remaining -= get_level_xp(level)
        level += 1
    return level, remaining


async def grant_xp(member, guild, amount, announce_channel=None):
    """Core XP-granting logic — shared by chat XP, voice XP, and the admin !addxp command.
    Handles (possibly several, if the XP jump is big) level-ups and level-up role rewards,
    both configured PER SERVER via !setlevelrole. Sends exactly ONE level-up message no
    matter how many levels were gained in one go. announce_channel is where level-up
    messages post if the server hasn't configured its own level-up channel."""
    guild_id = str(guild.id)
    user_id = str(member.id)
    guild_levels = levels_data.setdefault(guild_id, {})
    user_data = guild_levels.setdefault(user_id, {"xp": 0, "level": 0})
    user_data["xp"] += amount
    if user_data["xp"] < 0:
        user_data["xp"] = 0

    channel = get_guild_channel(guild.id, "level_up_channel") or announce_channel
    level_roles = guild_settings.get(guild_id, {}).get("level_roles", {})

    starting_level = user_data["level"]
    newly_earned_roles = []

    while user_data["xp"] >= get_level_xp(user_data["level"]):
        user_data["xp"] -= get_level_xp(user_data["level"])
        user_data["level"] += 1

        # Level-role rewards: PER SERVER, set with !setlevelrole, stored by role ID. Every
        # milestone passed through still grants its role — only the announcement is batched.
        reward_role_id = level_roles.get(str(user_data["level"]))
        if reward_role_id:
            reward_role = guild.get_role(reward_role_id)
            if reward_role and reward_role not in member.roles:
                try:
                    await member.add_roles(reward_role, reason=f"Reached level {user_data['level']}")
                    newly_earned_roles.append(reward_role)
                except discord.Forbidden:
                    await dm_owner(f"⚠️ Tried to give {member} the '{reward_role.name}' role in {guild.name} but don't have permission.")

    if user_data["level"] > starting_level and channel:
        if user_data["level"] == starting_level + 1:
            description = f"🎉 {member.mention} leveled up to **Level {user_data['level']}**!"
        else:
            description = f"🎉 {member.mention} leveled up from **Level {starting_level}** to **Level {user_data['level']}**!"
        embed = discord.Embed(description=description, color=discord.Color.green())
        if newly_earned_roles:
            embed.add_field(name="🏅 New role(s) earned", value=", ".join(r.mention for r in newly_earned_roles), inline=False)
        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            pass

    save_json(LEVELS_FILE, levels_data)


async def add_xp(message):
    guild_id = str(message.guild.id)
    if not guild_settings.get(guild_id, {}).get("leveling_enabled", True):
        return
    user_id = str(message.author.id)
    cooldown_key = f"{guild_id}:{user_id}"
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()

    if now - xp_cooldowns.get(cooldown_key, 0) < XP_COOLDOWN_SECONDS:
        return
    xp_cooldowns[cooldown_key] = now

    xp_amount = guild_settings.get(guild_id, {}).get("xp_per_message", XP_PER_MESSAGE)
    await grant_xp(message.author, message.guild, xp_amount, announce_channel=message.channel)


# ============================================================
# VOICE XP — awards XP for time spent actively in a voice channel with at least one
# other non-bot member. Rate is configurable per server (!setvoicexpamount, default
# DEFAULT_VOICE_XP_PER_MINUTE). Credited periodically via voice_xp_checkpoint() below
# (every VOICE_XP_CHECK_INTERVAL_MINUTES) so long sessions don't wait until someone
# leaves to get XP, and so a bot restart never loses more than one interval's credit.
# ============================================================
VOICE_XP_CHECK_INTERVAL_MINUTES = 5


async def award_voice_xp(guild, member, minutes_elapsed):
    if not guild_settings.get(str(guild.id), {}).get("leveling_enabled", True):
        return
    rate = guild_settings.get(str(guild.id), {}).get("voice_xp_per_minute", DEFAULT_VOICE_XP_PER_MINUTE)
    if rate <= 0 or minutes_elapsed <= 0:
        return
    await grant_xp(member, guild, int(rate * minutes_elapsed))


@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot:
        return
    key = f"{member.guild.id}:{member.id}"

    def channel_counts_as_active(channel):
        if channel is None:
            return False
        if channel == channel.guild.afk_channel:
            return False
        return len([m for m in channel.members if not m.bot]) >= 2

    was_active = channel_counts_as_active(before.channel)
    now_active = channel_counts_as_active(after.channel)

    if was_active and key in voice_sessions:
        elapsed_minutes = (datetime.datetime.now(datetime.timezone.utc).timestamp() - voice_sessions.pop(key)) / 60
        await award_voice_xp(member.guild, member, elapsed_minutes)

    if now_active:
        voice_sessions[key] = datetime.datetime.now(datetime.timezone.utc).timestamp()


@tasks.loop(minutes=VOICE_XP_CHECK_INTERVAL_MINUTES)
async def voice_xp_checkpoint():
    """Periodically pays out XP for everyone still actively in voice, so long sessions
    accrue XP without waiting for someone to leave the channel."""
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    for key in list(voice_sessions.keys()):
        guild_id_str, user_id_str = key.split(":")
        guild = bot.get_guild(int(guild_id_str))
        if guild is None:
            voice_sessions.pop(key, None)
            continue
        member = guild.get_member(int(user_id_str))
        voice_state = member.voice if member else None
        still_active = (
            member is not None and voice_state is not None and voice_state.channel is not None
            and voice_state.channel != guild.afk_channel
            and len([m for m in voice_state.channel.members if not m.bot]) >= 2
        )
        if not still_active:
            voice_sessions.pop(key, None)
            continue
        elapsed_minutes = (now - voice_sessions[key]) / 60
        voice_sessions[key] = now
        await award_voice_xp(guild, member, elapsed_minutes)


# ============================================================
# BIRTHDAYS — a birthday is one global fact per user (year isn't stored, just month/day —
# nobody needs a bot announcing their age). Each server picks its OWN announcement channel.
# ============================================================
def parse_birthday(text: str):
    """Accepts MM-DD or MM/DD. Returns 'MM-DD' string, or None if invalid."""
    text = text.strip().replace("/", "-")
    try:
        parsed = datetime.datetime.strptime(text, "%m-%d")
        return parsed.strftime("%m-%d")
    except ValueError:
        return None


def format_birthday(mmdd: str) -> str:
    """Turns a stored 'MM-DD' into a friendly form like 'May 14' for display."""
    parsed = datetime.datetime.strptime(mmdd, "%m-%d")
    return f"{parsed.strftime('%B')} {parsed.day}"


@bot.hybrid_command()
@discord.app_commands.describe(date="Your birthday as MM-DD, e.g. 04-20 (no year)")
async def setbirthday(ctx, date: str):
    """Sets YOUR birthday (month + day only — no year). Usage: !setbirthday 04-20 or !setbirthday 04/20"""
    parsed = parse_birthday(date)
    if not parsed:
        await ctx.send(embed=discord.Embed(description="Couldn't read that date — use `MM-DD`, e.g. `!setbirthday 04-20`.", color=discord.Color.red()))
        return
    birthdays_data[str(ctx.author.id)] = parsed
    save_json(BIRTHDAYS_FILE, birthdays_data)
    await ctx.send(embed=discord.Embed(description=f"🎂 Got it — your birthday is set to **{format_birthday(parsed)}**.", color=discord.Color.green()))


@bot.hybrid_command()
@discord.app_commands.describe(member="Whose birthday to show (leave blank for your own)")
async def birthday(ctx, member: discord.Member = None):
    """Shows your (or someone's) saved birthday."""
    member = member or ctx.author
    saved = birthdays_data.get(str(member.id))
    if not saved:
        await ctx.send(embed=discord.Embed(description=f"{member.mention} hasn't set a birthday yet." if member != ctx.author else "You haven't set a birthday yet — use `!setbirthday MM-DD`.", color=discord.Color.greyple()))
        return
    await ctx.send(embed=discord.Embed(description=f"🎂 {member.mention}'s birthday is **{format_birthday(saved)}**.", color=discord.Color.blurple()))


@bot.hybrid_command()
@commands.guild_only()
@has_permissions_or_owner(manage_guild=True)
@discord.app_commands.describe(channel="Channel where birthday announcements should post")
async def setbirthdaychannel(ctx, channel: discord.TextChannel):
    """Sets THIS server's birthday-announcement channel. Usage: !setbirthdaychannel #birthdays"""
    set_guild_channel(ctx.guild.id, "birthday_channel", channel.id)
    await ctx.send(embed=discord.Embed(description=f"✅ Birthday announcements will now post in {channel.mention}.", color=discord.Color.green()))


@tasks.loop(time=datetime.time(hour=9, minute=0, tzinfo=datetime.timezone.utc))
async def birthday_check():
    """Runs once a day. For every server with a birthday channel set, announces any current
    member whose saved birthday is today."""
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%m-%d")
    birthday_users = {uid for uid, bday in birthdays_data.items() if bday == today}
    if not birthday_users:
        return
    for guild in bot.guilds:
        channel = get_guild_channel(guild.id, "birthday_channel")
        if not channel:
            continue
        for user_id in birthday_users:
            member = guild.get_member(int(user_id))
            if member:
                embed = discord.Embed(description=f"🎂🎉 Happy Birthday {member.mention}! Hope it's a great one!", color=discord.Color.gold())
                try:
                    await channel.send(embed=embed)
                except discord.Forbidden:
                    pass


@bot.hybrid_command()
@commands.guild_only()
async def rank(ctx, member: discord.Member = None):
    """Shows your (or someone's) level in THIS server."""
    member = member or ctx.author
    data = levels_data.get(str(ctx.guild.id), {}).get(str(member.id), {"xp": 0, "level": 0})
    needed = get_level_xp(data["level"])
    progress = data["xp"] / needed if needed else 0
    bar_length = 20
    filled = int(bar_length * progress)
    bar = "█" * filled + "░" * (bar_length - filled)

    embed = discord.Embed(color=discord.Color.blurple())
    embed.set_author(name=f"{member.display_name}'s Rank in {ctx.guild.name}", icon_url=member.display_avatar.url)
    embed.add_field(name="Level", value=str(data["level"]), inline=True)
    embed.add_field(name="XP", value=f"{data['xp']} / {needed}", inline=True)
    embed.add_field(name="Progress", value=f"`{bar}`", inline=False)
    embed.set_thumbnail(url=member.display_avatar.url)
    await ctx.send(embed=embed)


class LeaderboardCategorySelect(discord.ui.Select):
    """A dropdown under the leaderboard. Only 'Overall XP' exists right now, but this is
    built so you can add more options later (e.g. Weekly XP, Voice XP) without redoing the UI —
    just add more SelectOptions and branch on interaction.data['values'][0] in the callback."""
    def __init__(self):
        options = [discord.SelectOption(label="Overall XP", value="overall", emoji="⭐", default=True)]
        super().__init__(placeholder="Overall XP", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            "Only **Overall XP** is tracked right now — more categories may come later!",
            ephemeral=True,
        )


class LeaderboardView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(LeaderboardCategorySelect())


@bot.hybrid_command()
@commands.guild_only()
async def leaderboard(ctx):
    """Shows the top 10 in THIS server only, styled as a compact ranked list with a category dropdown."""
    guild_levels = levels_data.get(str(ctx.guild.id), {})
    if not guild_levels:
        await ctx.send(embed=discord.Embed(description="No one has earned XP yet in this server!", color=discord.Color.greyple()))
        return
    sorted_users = sorted(guild_levels.items(), key=lambda x: (x[1]["level"], x[1]["xp"]), reverse=True)[:10]

    embed = discord.Embed(color=discord.Color.dark_teal())
    embed.set_author(
        name=f"{ctx.guild.name}'s xp leaderboard",
        icon_url=(ctx.guild.icon.url if ctx.guild.icon else bot.user.display_avatar.url),
    )

    intro = "Want to view more than the top 10 users?"
    if LEADERBOARD_WEBSITE_URL:
        intro += f" [Click here]({LEADERBOARD_WEBSITE_URL})"
    embed.description = intro

    for i, (user_id, data) in enumerate(sorted_users, start=1):
        embed.add_field(name="\u200b", value=f"**#{i}** • <@{user_id}> • LVL: {data['level']}", inline=False)

    embed.set_footer(text=f"Top {len(sorted_users)} members by level — this server only")
    await ctx.send(embed=embed, view=LeaderboardView())


@bot.hybrid_command()
async def globalleaderboard(ctx):
    """Combines everyone's XP across EVERY server the bot is in.
    A user's XP from each server is added together, then converted back into one overall level."""
    combined_totals = {}
    for guild_id, users in levels_data.items():
        for user_id, data in users.items():
            combined_totals[user_id] = combined_totals.get(user_id, 0) + total_xp_for(data["level"], data["xp"])

    if not combined_totals:
        await ctx.send(embed=discord.Embed(description="No one has earned XP anywhere yet!", color=discord.Color.greyple()))
        return

    sorted_users = sorted(combined_totals.items(), key=lambda x: x[1], reverse=True)[:10]
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = []
    for i, (user_id, total) in enumerate(sorted_users, start=1):
        level, _ = level_from_total(total)
        user = await bot.fetch_user(int(user_id))
        prefix = medals.get(i, f"`#{i}`")
        lines.append(f"{prefix} **{user.name}** — Level {level} (combined)")

    embed = discord.Embed(title="🌐 Global Leaderboard", description="\n".join(lines), color=discord.Color.purple())
    embed.set_footer(text="Combines XP from every server I'm in — higher levels take more XP to reach, so totals aren't just added levels")
    await ctx.send(embed=embed)


# ============================================================
# GIVEAWAYS — react-to-enter, timed, with multiple possible winners. Ended
# automatically by a background loop (giveaway_check), or early with !gend.
# ============================================================
DURATION_RE = re.compile(r"(\d+)\s*(d|h|m|s)", re.IGNORECASE)
DURATION_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(text: str):
    """Parses durations like '10m', '2h', '1d', or combined '1d12h'. Returns total seconds,
    or None if nothing valid was found."""
    matches = DURATION_RE.findall(text.strip().lower())
    if not matches:
        return None
    total = sum(int(amount) * DURATION_UNIT_SECONDS[unit] for amount, unit in matches)
    return total if total > 0 else None


@bot.hybrid_command()
@commands.guild_only()
@has_permissions_or_owner(manage_guild=True)
async def giveaway(ctx, duration: str, winners: int, *, prize: str):
    """Starts a giveaway. Usage: !giveaway 1h 1 Nitro Classic
    Duration examples: 30s, 10m, 2h, 1d, or combined like 1d12h."""
    seconds = parse_duration(duration)
    if not seconds:
        await ctx.send(embed=discord.Embed(description="Couldn't read that duration — try `10m`, `2h`, `1d`, or `1d12h`.", color=discord.Color.red()))
        return
    if winners < 1:
        await ctx.send(embed=discord.Embed(description="Needs at least 1 winner.", color=discord.Color.red()))
        return

    end_time = datetime.datetime.now(datetime.timezone.utc).timestamp() + seconds
    embed = discord.Embed(
        title="🎉 GIVEAWAY 🎉",
        description=(f"**{prize}**\n\nReact with 🎉 to enter!\n"
                      f"Ends: <t:{int(end_time)}:R>\nWinners: **{winners}**\nHosted by: {ctx.author.mention}"),
        color=discord.Color.fuchsia(),
    )
    msg = await ctx.send(embed=embed)
    await msg.add_reaction("🎉")

    giveaways_data[str(msg.id)] = {
        "guild_id": ctx.guild.id, "channel_id": ctx.channel.id, "prize": prize,
        "winners": winners, "end_time": end_time, "host_id": ctx.author.id,
    }
    save_json(GIVEAWAYS_FILE, giveaways_data)


async def end_giveaway(message_id: str, data: dict):
    guild = bot.get_guild(data["guild_id"])
    channel = guild.get_channel(data["channel_id"]) if guild else None
    if channel is None:
        giveaways_data.pop(message_id, None)
        save_json(GIVEAWAYS_FILE, giveaways_data)
        return
    try:
        message = await channel.fetch_message(int(message_id))
    except (discord.NotFound, discord.Forbidden):
        giveaways_data.pop(message_id, None)
        save_json(GIVEAWAYS_FILE, giveaways_data)
        return

    reaction = discord.utils.get(message.reactions, emoji="🎉")
    entrants = [user async for user in reaction.users() if not user.bot] if reaction else []

    if entrants:
        pick_count = min(data["winners"], len(entrants))
        pick = random.sample(entrants, pick_count)
        winner_mentions = ", ".join(w.mention for w in pick)
        result_text = f"🎉 Congrats {winner_mentions}! You won **{data['prize']}**!"
    else:
        winner_mentions = "None — no valid entries"
        result_text = f"No valid entries — nobody won **{data['prize']}**."

    ended_embed = discord.Embed(
        title="🎉 GIVEAWAY ENDED 🎉",
        description=f"**{data['prize']}**\n\nWinner(s): {winner_mentions}",
        color=discord.Color.dark_grey(),
    )
    try:
        await message.edit(embed=ended_embed)
    except discord.Forbidden:
        pass
    await channel.send(embed=discord.Embed(description=result_text, color=discord.Color.gold()))

    giveaways_data.pop(message_id, None)
    save_json(GIVEAWAYS_FILE, giveaways_data)


@tasks.loop(seconds=30)
async def giveaway_check():
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    ended = [mid for mid, data in giveaways_data.items() if data["end_time"] <= now]
    for message_id in ended:
        data = giveaways_data.get(message_id)
        if data:
            await end_giveaway(message_id, data)


@bot.hybrid_command()
@has_permissions_or_owner(manage_guild=True)
async def gend(ctx, message_id: str):
    """Ends a giveaway early. Usage: !gend <message_id>"""
    data = giveaways_data.get(message_id)
    if not data:
        await ctx.send(embed=discord.Embed(description="No active giveaway with that message ID.", color=discord.Color.red()))
        return
    await end_giveaway(message_id, data)
    await ctx.send(embed=discord.Embed(description="✅ Giveaway ended.", color=discord.Color.green()))


@bot.hybrid_command()
@has_permissions_or_owner(manage_guild=True)
async def greroll(ctx, message_id: str):
    """Re-picks one winner for an ALREADY-ENDED giveaway. Run this in the same channel
    the giveaway was posted in. Usage: !greroll <message_id>"""
    try:
        message = await ctx.channel.fetch_message(int(message_id))
    except (discord.NotFound, discord.Forbidden, ValueError):
        await ctx.send(embed=discord.Embed(description="Couldn't find that message in this channel.", color=discord.Color.red()))
        return

    reaction = discord.utils.get(message.reactions, emoji="🎉")
    entrants = [user async for user in reaction.users() if not user.bot] if reaction else []
    if not entrants:
        await ctx.send(embed=discord.Embed(description="No valid entrants to reroll from.", color=discord.Color.red()))
        return

    winner = random.choice(entrants)
    await ctx.send(embed=discord.Embed(description=f"🎉 New winner: {winner.mention}!", color=discord.Color.fuchsia()))


# ============================================================
# PERSONALITY CHAT — hybrid: real AI when available, free fallback when not
# ============================================================
# FREE MODE: no API key, no cost, ever. Keyword-matched + randomized so it doesn't
# feel too robotic, but it can't truly "understand" like an AI can — it's pattern matching.
FREE_MODE_RESPONSES = {
    "hello": ["Heyyy! 👋", "Hiii, what's up?", "Hey hey!"],
    "hi": ["Hi there! 😄", "Heyo!"],
    "how are you": ["I'm just a bunch of code but I'm vibing 😌", "Doing great, thanks for asking!"],
    "bye": ["See ya! 👋", "Later!"],
    "thanks": ["Anytime! 💫", "You got it!"],
    "thank you": ["No problem at all!", "Happy to help!"],
    "love you": ["Aww 🥹💜", "Love you too!"],
    "good bot": ["I try my best! 🥰", "Aww thank you!"],
}
FREE_MODE_OWNER_EXTRA = {
    "hello": ["Heyyy it's you!! 💜", "Ahh hi hi, missed you!"],
    "hi": ["Hii you're back! 🥰"],
    "love you": ["I love you more!! 💜💜", "You're my favorite person, obviously 🥹"],
}
FREE_MODE_DEFAULT = ["Hmm, not sure what to say to that! 😅", "I hear you! Tell me more?", "Interesting... go on 👀"]
FREE_MODE_DEFAULT_OWNER = ["Ooh not sure what to say to that, but I'm listening! 💜", "Tell me more? 🥰"]

# Shown when someone just @mentions her with NO other text — a "bare ping".
# These always fire regardless of AI/free mode since there's no real message to respond to.
PLAIN_PING_RESPONSES = ["What's up? 👋", "Yo, you rang?", "Sup!", "You called? 👀"]
PLAIN_PING_OWNER_RESPONSES = ["Heyyy it's you!! What's up? 💜", "Ooh, hi!! What do you need? 🥰", "You rang, boss? 😄"]


def is_plain_ping(message: discord.Message) -> bool:
    """True if the message is ONLY a mention of the bot with no other text."""
    content = message.content
    for user in message.mentions:
        content = content.replace(f"<@{user.id}>", "").replace(f"<@!{user.id}>", "")
    return content.strip() == ""



def free_mode_reply(content: str, is_owner: bool) -> str:
    content_lower = content.lower()
    for keyword, responses in FREE_MODE_RESPONSES.items():
        if keyword in content_lower:
            if is_owner and keyword in FREE_MODE_OWNER_EXTRA:
                return random.choice(FREE_MODE_OWNER_EXTRA[keyword])
            return random.choice(responses)
    return random.choice(FREE_MODE_DEFAULT_OWNER if is_owner else FREE_MODE_DEFAULT)


async def ask_ai(message: discord.Message):
    is_owner = message.author.id == OWNER_ID

    # No key set at all → skip straight to free mode, don't even try the API
    if not ANTHROPIC_API_KEY:
        return free_mode_reply(message.content, is_owner)

    system_prompt = BOT_PERSONALITY
    if is_owner:
        system_prompt += (
            f"\n\nThe person talking to you right now is {BOT_NAME}'s owner — the person who made you. "
            "Be extra warm, affectionate, and a little playful with them specifically, like a pet who's "
            "excited to see their person. With everyone else, stay friendly but more neutral."
        )

    try:
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": AI_MODEL,
                "max_tokens": 300,
                "system": system_prompt,
                "messages": [{"role": "user", "content": message.content}],
            },
            timeout=15,
        )
        data = response.json()

        # If the API call failed (out of credits, bad key, rate limited, etc), data won't have "content"
        if response.status_code != 200 or "content" not in data:
            await dm_owner(f"⚠️ AI call failed ({response.status_code}), used free-mode reply instead. Details: {data}")
            return free_mode_reply(message.content, is_owner)

        return data["content"][0]["text"]

    except Exception as e:
        await dm_owner(f"⚠️ AI chat error, used free-mode reply instead: {e}")
        return free_mode_reply(message.content, is_owner)


# ============================================================
# MESSAGE HANDLING (automod + XP + AI/free chat)
# ============================================================
@bot.event
async def on_message(message):
    if message.author.bot:
        return

    if REQUIRE_OWNER_PRESENT and not await owner_in_guild(message.guild):
        return  # bricked — owner isn't in this server, do nothing at all

    # Everything in this block is server-only moderation/leveling logic — message.guild is
    # None for DMs, so none of it applies (and touching message.guild.id there would crash).
    if message.guild is not None:
        if contains_banned_word(message.content, message.guild.id):
            await message.delete()
            await message.channel.send(embed=discord.Embed(description=f"{message.author.mention}, that language isn't allowed here.", color=discord.Color.red()), delete_after=5)
            await dm_owner(f"🚫 Deleted a message from **{message.author}** in #{message.channel} (banned word):\n> {message.content}")
            return

        if len(message.mentions) >= 5:
            await message.delete()
            await message.channel.send(embed=discord.Embed(description=f"{message.author.mention}, mass-pinging isn't allowed.", color=discord.Color.red()), delete_after=5)
            await dm_owner(f"🚫 Deleted a mass-mention message from **{message.author}** in #{message.channel}")
            return

        now = datetime.datetime.now(datetime.timezone.utc).timestamp()
        timestamps = [t for t in spam_tracker.get(message.author.id, []) if now - t < SPAM_SECONDS]
        timestamps.append(now)
        spam_tracker[message.author.id] = timestamps

        if len(timestamps) > SPAM_LIMIT:
            spam_tracker[message.author.id] = []
            try:
                await custom_timeout(message.author, message.guild, minutes=5, reason="Spamming")
                await message.channel.send(embed=discord.Embed(description=f"⏱️ {message.author.mention} was timed out for spamming.", color=discord.Color.orange()))
            except Exception as e:
                await dm_owner(f"⚠️ Failed to auto-timeout {message.author}: {e}")
            return

        await add_xp(message)
        await handle_afk(message)

    is_mentioned = bot.user in message.mentions
    is_reply_to_bot = (
        message.reference and message.reference.resolved
        and getattr(message.reference.resolved, "author", None) == bot.user
    )

    if is_mentioned and is_plain_ping(message):
        is_owner = message.author.id == OWNER_ID
        reply = random.choice(PLAIN_PING_OWNER_RESPONSES if is_owner else PLAIN_PING_RESPONSES)
        await message.reply(reply)
        return

    if is_mentioned or is_reply_to_bot:
        async with message.channel.typing():
            reply = await ask_ai(message)
        await message.reply(reply)
        return

    await bot.process_commands(message)


# ============================================================
# CUSTOM TIMEOUT SYSTEM
# ============================================================
async def custom_timeout(member: discord.Member, guild: discord.Guild, minutes: int, reason: str = "No reason given", moderator=None):
    timeout_role = discord.utils.get(guild.roles, name=TIMEOUT_ROLE_NAME)
    timeout_channel = get_guild_channel(guild.id, "timeout_channel")

    if timeout_role is None:
        timeout_role = await guild.create_role(name=TIMEOUT_ROLE_NAME, reason="Auto-created for timeout system")
        for channel in guild.channels:
            try:
                if timeout_channel and channel.id == timeout_channel.id:
                    # The timeout channel itself: they CAN see it, but can't talk/speak/react in it
                    await channel.set_permissions(timeout_role, view_channel=True, send_messages=False, speak=False, add_reactions=False)
                else:
                    # Every other channel: fully hidden from them
                    await channel.set_permissions(timeout_role, view_channel=False, send_messages=False, speak=False)
            except discord.Forbidden:
                pass

    current_role_ids = [role.id for role in member.roles if role != guild.default_role]
    stored_roles[str(member.id)] = current_role_ids
    save_json(ROLES_FILE, stored_roles)

    roles_to_remove = [r for r in member.roles if r != guild.default_role]
    await member.remove_roles(*roles_to_remove, reason=reason)
    await member.add_roles(timeout_role, reason=reason)

    await mod_log(guild, "Member Timed Out", member, moderator or bot.user, f"{reason} ({minutes} min)", discord.Color.orange())

    if timeout_channel:
        embed = discord.Embed(
            title="⏱️ Member Timed Out",
            description=f"{member.mention} has been timed out for **{minutes} minute(s)**.\nReason: {reason}",
            color=discord.Color.orange(),
        )
        await timeout_channel.send(embed=embed)

    await asyncio.sleep(minutes * 60)
    await restore_roles(member, guild)


async def restore_roles(member: discord.Member, guild: discord.Guild):
    saved_ids = stored_roles.get(str(member.id))
    timeout_role = discord.utils.get(guild.roles, name=TIMEOUT_ROLE_NAME)

    if timeout_role and timeout_role in member.roles:
        await member.remove_roles(timeout_role, reason="Timeout expired")

    if saved_ids:
        roles = [guild.get_role(rid) for rid in saved_ids if guild.get_role(rid)]
        if roles:
            await member.add_roles(*roles, reason="Timeout expired — restoring roles")
        del stored_roles[str(member.id)]
        save_json(ROLES_FILE, stored_roles)

    await mod_log(guild, "Timeout Expired — Roles Restored", member, bot.user, "Automatic", discord.Color.green())


# ============================================================
# MOD COMMANDS
# ============================================================
@bot.hybrid_command()
@commands.guild_only()
@has_permissions_or_owner(moderate_members=True)
async def timeout(ctx, member: discord.Member, minutes: int, *, reason="No reason given"):
    await custom_timeout(member, ctx.guild, minutes, reason, moderator=ctx.author)
    await ctx.send(embed=discord.Embed(description=f"🔇 {member.mention} has been timed out for {minutes} minute(s).\nReason: {reason}", color=discord.Color.orange()))


@bot.hybrid_command()
@commands.guild_only()
@has_permissions_or_owner(moderate_members=True)
async def untimeout(ctx, member: discord.Member):
    await restore_roles(member, ctx.guild)
    await ctx.send(embed=discord.Embed(description=f"🔊 {member.mention}'s roles have been restored.", color=discord.Color.green()))


@bot.hybrid_command()
@commands.guild_only()
@has_permissions_or_owner(manage_messages=True)
async def clear(ctx, amount: int):
    """Deletes the last <amount> messages in this channel. Usage: !clear 20"""
    if amount < 1 or amount > 500:
        await ctx.send(embed=discord.Embed(description="Pick a number between 1 and 500.", color=discord.Color.red()), delete_after=5)
        return
    deleted = await ctx.channel.purge(limit=amount + 1)  # +1 accounts for the command message itself
    confirmation = await ctx.send(embed=discord.Embed(description=f"🧹 Deleted {len(deleted) - 1} messages.", color=discord.Color.orange()))
    await confirmation.delete(delay=4)
    await mod_log(ctx.guild, "Messages Cleared", ctx.channel, ctx.author, f"{len(deleted) - 1} messages in #{ctx.channel.name}", discord.Color.orange())


@bot.hybrid_command()
@commands.guild_only()
@has_permissions_or_owner(manage_messages=True)
async def clearuser(ctx, member: discord.Member, amount: int = 100):
    """Deletes messages from a specific user (scans the last <amount> messages, default 100).
    Usage: !clearuser @user 50"""
    if amount < 1 or amount > 1000:
        await ctx.send(embed=discord.Embed(description="Pick a scan amount between 1 and 1000.", color=discord.Color.red()), delete_after=5)
        return

    def check(m):
        return m.author.id == member.id

    deleted = await ctx.channel.purge(limit=amount, check=check)
    confirmation = await ctx.send(embed=discord.Embed(description=f"🧹 Deleted {len(deleted)} messages from **{member}**.", color=discord.Color.orange()))
    await confirmation.delete(delay=4)
    await mod_log(ctx.guild, "Messages Cleared (by user)", member, ctx.author, f"{len(deleted)} messages in #{ctx.channel.name}", discord.Color.orange())


@bot.hybrid_command()
@commands.guild_only()
@has_permissions_or_owner(manage_messages=True)
async def clearkeyword(ctx, keyword: str, amount: int = 100):
    """Deletes messages containing a specific word/phrase (scans the last <amount> messages, default 100).
    Usage: !clearkeyword "some phrase" 50"""
    if amount < 1 or amount > 1000:
        await ctx.send(embed=discord.Embed(description="Pick a scan amount between 1 and 1000.", color=discord.Color.red()), delete_after=5)
        return

    keyword_lower = keyword.lower()

    def check(m):
        return keyword_lower in m.content.lower()

    deleted = await ctx.channel.purge(limit=amount, check=check)
    confirmation = await ctx.send(embed=discord.Embed(description=f"🧹 Deleted {len(deleted)} messages containing `{keyword}`.", color=discord.Color.orange()))
    await confirmation.delete(delay=4)
    await mod_log(ctx.guild, "Messages Cleared (by keyword)", ctx.channel, ctx.author, f"{len(deleted)} messages matching '{keyword}' in #{ctx.channel.name}", discord.Color.orange())


@bot.hybrid_command()
@commands.guild_only()
@has_permissions_or_owner(kick_members=True)
async def kick(ctx, member: discord.Member, *, reason="No reason given"):
    dm_note = ""
    try:
        await member.send(embed=discord.Embed(description=f"👢 You were kicked from **{ctx.guild.name}**.\nReason: {reason}", color=discord.Color.red()))
    except discord.Forbidden:
        dm_note = "\n⚠️ DM_CLOSED: Couldn't notify them by DM — their DMs are closed."
    await member.kick(reason=reason)
    await ctx.send(embed=discord.Embed(description=f"👢 Kicked {member.mention}.\nReason: {reason}{dm_note}", color=discord.Color.red()))
    await mod_log(ctx.guild, "Member Kicked", member, ctx.author, reason, discord.Color.red())


@bot.hybrid_command()
@commands.guild_only()
@has_permissions_or_owner(ban_members=True)
async def ban(ctx, member: discord.Member, *, reason="No reason given"):
    dm_note = ""
    try:
        await member.send(embed=discord.Embed(description=f"🔨 You were banned from **{ctx.guild.name}**.\nReason: {reason}", color=discord.Color.dark_red()))
    except discord.Forbidden:
        dm_note = "\n⚠️ DM_CLOSED: Couldn't notify them by DM — their DMs are closed."
    await member.ban(reason=reason)
    await ctx.send(embed=discord.Embed(description=f"🔨 Banned {member.mention}.\nReason: {reason}{dm_note}", color=discord.Color.dark_red()))
    # Note: on_member_ban also fires and logs this via the audit log — that's fine as a backup,
    # duplicate log entries just mean extra confirmation.


# ============================================================
# SERVER BACKUP / RESTORE
# Saves a server's roles + channel structure to a file, and can recreate
# that structure in another server. Only works where the bot already has
# real admin access — invited normally to both servers by their owners.
# It restores STRUCTURE (roles, channels, categories, permissions) —
# it does NOT restore messages, members, or who's in which role.
# ============================================================
BACKUPS_FOLDER = os.path.join(BASE_DIR, "backups")
os.makedirs(BACKUPS_FOLDER, exist_ok=True)


def guild_backup_folder(guild_id):
    """Each server gets its own backup subfolder, so a server owner using these commands
    can only ever see or restore backups made FROM their own server."""
    folder = os.path.join(BACKUPS_FOLDER, str(guild_id))
    os.makedirs(folder, exist_ok=True)
    return folder


def normalize_backup_name(name: str) -> str:
    """Backup names are matched case-insensitively — 'Departure', 'departure', and 'DEPARTURE'
    are all the same save. This normalized form is what's actually used as the filename."""
    return name.strip().lower()


def list_backup_names(guild_id):
    """Returns the DISPLAY name (as it was originally typed) of every backup saved for this
    server — even though the files on disk are keyed by the case-insensitive normalized name."""
    folder = guild_backup_folder(guild_id)
    names = []
    for filename in os.listdir(folder):
        if not filename.endswith(".json"):
            continue
        data = load_json(os.path.join(folder, filename))
        names.append(data.get("display_name", filename[:-5]))
    return names


def build_backup_data(guild, display_name):
    """Snapshots this server's current roles, categories, channels (with permission overwrites),
    and who has which custom role. Shared by the manual !backupserver command and the automatic
    background re-sync, so both always save in exactly the same shape."""
    data = {"display_name": display_name, "roles": [], "categories": [], "channels": [], "member_roles": {}}

    # Roles (skip @everyone and the bot's own managed roles, bottom to top so restore order is right)
    for role in sorted(guild.roles, key=lambda r: r.position):
        if role.is_default() or role.managed:
            continue
        data["roles"].append({
            "name": role.name,
            "color": role.color.value,
            "permissions": role.permissions.value,
            "hoist": role.hoist,
            "mentionable": role.mentionable,
        })

    saved_role_names = {r["name"] for r in data["roles"]}

    # Categories
    for category in guild.categories:
        data["categories"].append({"name": category.name, "position": category.position})

    # Channels (text + voice), including per-role permission overwrites (view/send/etc)
    for channel in guild.channels:
        if isinstance(channel, discord.CategoryChannel):
            continue
        entry = {
            "name": channel.name,
            "type": str(channel.type),
            "category": channel.category.name if channel.category else None,
            "position": channel.position,
            "overwrites": [],
        }
        if isinstance(channel, discord.TextChannel):
            entry["topic"] = channel.topic

        # Save permission overwrites, keyed by role NAME (not ID, since IDs won't match after recreation)
        for target, overwrite in channel.overwrites.items():
            if isinstance(target, discord.Role):
                allow, deny = overwrite.pair()
                entry["overwrites"].append({
                    "role_name": target.name if not target.is_default() else "@everyone",
                    "allow": allow.value,
                    "deny": deny.value,
                })
        data["channels"].append(entry)

    # Who has which custom (non-default, non-managed) role — so restore can re-assign them
    for member in guild.members:
        member_role_names = [r.name for r in member.roles if r.name in saved_role_names]
        if member_role_names:
            data["member_roles"][str(member.id)] = member_role_names

    return data


def save_backup(guild, normalized_name, display_name):
    """Builds and writes a backup to disk, returning the data that was saved."""
    data = build_backup_data(guild, display_name)
    path = os.path.join(guild_backup_folder(guild.id), f"{normalized_name}.json")
    save_json(path, data)
    return data


# --- Auto-sync: once a server has a backup, keep it automatically current -------------------
# Whenever !backupserver is run, that save becomes THIS server's "auto-synced" backup — from
# then on, role/channel changes (made through Discord's UI OR the bot's own commands) quietly
# re-save it in the background, so there's no need to keep re-running !backupserver by hand.
# Only one save per server auto-syncs at a time (whichever was most recently backed up);
# !autobackup off turns it off if you'd rather keep a save as a frozen snapshot instead.
auto_backup_tasks = {}  # guild_id -> asyncio.Task, debounces bursts of role/channel events


async def perform_auto_backup(guild):
    settings = guild_settings.get(str(guild.id), {})
    normalized = settings.get("auto_backup_name")
    if not normalized:
        return
    path = os.path.join(guild_backup_folder(guild.id), f"{normalized}.json")
    existing = load_json(path) if os.path.exists(path) else {}
    display_name = existing.get("display_name", normalized)
    save_backup(guild, normalized, display_name)


async def _debounced_auto_backup(guild):
    await asyncio.sleep(10)  # let a burst of changes (bulk edits, a restore) settle before saving
    try:
        await perform_auto_backup(guild)
    except Exception as e:
        print(f"⚠️ Auto-backup sync failed for {guild.name}: {e}")


def schedule_auto_backup(guild):
    """Call this from any event that changes server structure. No-ops if this server doesn't
    have an auto-synced backup set up yet."""
    if guild is None:
        return
    if not guild_settings.get(str(guild.id), {}).get("auto_backup_name"):
        return
    existing = auto_backup_tasks.get(guild.id)
    if existing and not existing.done():
        existing.cancel()
    auto_backup_tasks[guild.id] = bot.loop.create_task(_debounced_auto_backup(guild))


@bot.hybrid_command()
@commands.guild_only()
@backup_permission()
async def backupserver(ctx, backup_name: str):
    """Saves this server's roles, channels (with permission overwrites), and who has which
    custom role, to a file — and marks it as the save that stays automatically kept in sync
    with future role/channel changes here. Usage: !backupserver mybackup. Usable by you (the
    bot owner, in any server) or by that server's own owner (for their own server only)."""
    guild = ctx.guild
    normalized = normalize_backup_name(backup_name)
    data = save_backup(guild, normalized, backup_name.strip())

    guild_settings.setdefault(str(guild.id), {})["auto_backup_name"] = normalized
    save_json(GUILD_SETTINGS_FILE, guild_settings)

    all_backups = list_backup_names(guild.id)
    embed = discord.Embed(
        title="💾 Server Backed Up",
        description=(f"Saved as `{data['display_name']}` — {len(data['roles'])} roles, {len(data['categories'])} categories, "
                      f"{len(data['channels'])} channels (with permissions), {len(data['member_roles'])} members' role assignments."),
        color=discord.Color.green(),
    )
    embed.add_field(name="📁 Your saves", value=", ".join(f"`{b}`" for b in all_backups), inline=False)
    embed.set_footer(text="🔄 This save now stays automatically synced with role/channel changes here — no need to re-run this manually. (Only the bot's creator can toggle auto-sync on/off.)")
    await ctx.send(embed=embed)


@bot.hybrid_command()
@commands.guild_only()
@owner_only()
@discord.app_commands.describe(
    mode="Turn auto-sync on for an existing backup, off, or just check status",
    backup_name="Which saved backup to auto-sync (only needed for 'on')",
)
async def autobackup(ctx, mode: typing.Literal["status", "on", "off"] = "status", backup_name: str = None):
    """Shows, enables, or disables which backup is kept automatically in sync with THIS
    server's live roles/channels. Bot-creator only — server owners can still make and restore
    backups with !backupserver / !restoreserver, but only you can flip auto-sync on or off.
    Usage:
      !autobackup                     — check what's currently auto-syncing
      !autobackup on <name>           — turn auto-sync on for an existing backup
      !autobackup off                 — turn auto-sync off"""
    settings = guild_settings.get(str(ctx.guild.id), {})
    current = settings.get("auto_backup_name")

    if mode == "off":
        guild_settings.setdefault(str(ctx.guild.id), {}).pop("auto_backup_name", None)
        save_json(GUILD_SETTINGS_FILE, guild_settings)
        await ctx.send(embed=discord.Embed(description="🛑 Turned off auto-sync for this server. Your existing saves are untouched.", color=discord.Color.orange()))
        return

    if mode == "on":
        target = backup_name or current
        if not target:
            await ctx.send(embed=discord.Embed(description="Tell me which backup to auto-sync — e.g. `!autobackup on mybackup`. Run `!listbackups` to see your saves.", color=discord.Color.red()))
            return
        normalized = normalize_backup_name(target)
        path = os.path.join(guild_backup_folder(ctx.guild.id), f"{normalized}.json")
        if not os.path.exists(path):
            all_backups = list_backup_names(ctx.guild.id)
            await ctx.send(embed=discord.Embed(
                description=f"❌ No backup found named `{target}`.\n📁 Your saves: {', '.join(f'`{b}`' for b in all_backups) if all_backups else '(none yet — use `!backupserver <name>` first)'}",
                color=discord.Color.red(),
            ))
            return
        guild_settings.setdefault(str(ctx.guild.id), {})["auto_backup_name"] = normalized
        save_json(GUILD_SETTINGS_FILE, guild_settings)
        display_name = load_json(path).get("display_name", target)
        await ctx.send(embed=discord.Embed(description=f"🔄 `{display_name}` will now stay automatically synced with this server's roles and channels.", color=discord.Color.green()))
        return

    # mode == "status"
    if current:
        await ctx.send(embed=discord.Embed(description=f"🔄 `{current}` is being kept automatically up to date with this server's roles and channels.", color=discord.Color.blurple()))
    else:
        await ctx.send(embed=discord.Embed(description="No backup is auto-syncing here right now — run `!autobackup on <name>` or `!backupserver <name>` to start one.", color=discord.Color.greyple()))


@bot.hybrid_command()
@commands.guild_only()
@backup_permission()
@discord.app_commands.describe(
    backup_name="Which saved backup to restore (leave blank to list your saves)",
    what="Restore everything, or only roles, or only channels/categories",
)
async def restoreserver(
    ctx,
    backup_name: str = None,
    what: typing.Literal["all", "roles", "channels"] = "all",
):
    """Recreates roles and/or channels (with permissions) from a saved backup INTO THIS server.
    Usage: !restoreserver mybackup [all|roles|channels]
    - all (default): roles + categories + channels + member role assignments
    - roles: only recreates roles and re-assigns them to current members — no channels/categories
    - channels: only recreates categories + channels (with permission overwrites) — no roles,
      no member role re-assignment. If a channel's saved overwrite references a role that isn't
      already in this server, that specific overwrite is just skipped.
    Run with no name to see your current saves. Usable by you (the bot owner, in any server) or by
    that server's own owner (for their own server only) — restores only pull from THIS server's saves.
    Only rebuilds structure + role assignments — does not restore messages."""
    all_backups = list_backup_names(ctx.guild.id)

    if backup_name is None:
        embed = discord.Embed(title="📁 Your Saves", color=discord.Color.blurple())
        if not all_backups:
            embed.description = "You don't have any backups saved yet — use `!backupserver <name>` first."
        else:
            embed.description = "\n".join(f"- `{b}`" for b in all_backups) + "\n\nRun `!restoreserver <name>` to restore one (add `roles` or `channels` to only restore part of it)."
        await ctx.send(embed=embed)
        return

    path = os.path.join(guild_backup_folder(ctx.guild.id), f"{normalize_backup_name(backup_name)}.json")
    if not os.path.exists(path):
        embed = discord.Embed(
            description=f"❌ No backup found named `{backup_name}`.\n📁 Your saves: {', '.join(f'`{b}`' for b in all_backups) if all_backups else '(none yet)'}",
            color=discord.Color.red(),
        )
        await ctx.send(embed=embed)
        return

    data = load_json(path)
    guild = ctx.guild
    backup_name = data.get("display_name", backup_name)
    restore_roles = what in ("all", "roles")
    restore_channels = what in ("all", "channels")
    what_label = {"all": "everything", "roles": "roles only", "channels": "channels only"}[what]
    await ctx.send(embed=discord.Embed(description=f"🔧 Restoring `{backup_name}` ({what_label}) into **{guild.name}**... this may take a bit.", color=discord.Color.blurple()))

    # Recreate roles first (bottom to top, matches saved order), keep a name -> role object map.
    # If we're not restoring roles this run, role_map stays empty — channel overwrites that
    # reference a role by name simply get skipped below (they'll match existing roles by name
    # if that role already exists in the server from a prior restore).
    role_map = {}
    if restore_roles:
        for role_data in data["roles"]:
            new_role = await guild.create_role(
                name=role_data["name"],
                color=discord.Color(role_data["color"]),
                permissions=discord.Permissions(role_data["permissions"]),
                hoist=role_data["hoist"],
                mentionable=role_data["mentionable"],
                reason=f"Server restore from backup '{backup_name}' (roles)",
            )
            role_map[role_data["name"]] = new_role
    elif restore_channels:
        # Channels-only restore: match overwrites against roles that already exist in this
        # server by name, so a prior roles-only restore (or existing roles) still get wired up.
        role_map = {role.name: role for role in guild.roles}

    # Recreate categories, keep a name -> object map for channel placement
    category_map = {}
    if restore_channels:
        for cat_data in data["categories"]:
            cat = await guild.create_category(cat_data["name"], reason=f"Server restore from backup '{backup_name}' (channels)")
            category_map[cat_data["name"]] = cat

    # Recreate channels into their categories, then re-apply saved permission overwrites
    if restore_channels:
        for chan_data in data["channels"]:
            category = category_map.get(chan_data["category"])
            if chan_data["type"] == "voice":
                new_channel = await guild.create_voice_channel(chan_data["name"], category=category, reason=f"Server restore from backup '{backup_name}' (channels)")
            else:
                new_channel = await guild.create_text_channel(
                    chan_data["name"], category=category, topic=chan_data.get("topic"),
                    reason=f"Server restore from backup '{backup_name}' (channels)"
                )

            for ow in chan_data.get("overwrites", []):
                target = guild.default_role if ow["role_name"] == "@everyone" else role_map.get(ow["role_name"])
                if target is None:
                    continue
                overwrite = discord.PermissionOverwrite.from_pair(
                    discord.Permissions(ow["allow"]), discord.Permissions(ow["deny"])
                )
                try:
                    await new_channel.set_permissions(target, overwrite=overwrite, reason=f"Server restore from backup '{backup_name}' (channels)")
                except discord.Forbidden:
                    pass

    # Re-assign saved roles to any current member who had one — only relevant when roles were
    # actually part of this restore.
    restored_members = 0
    if restore_roles:
        for member_id, role_names in data.get("member_roles", {}).items():
            member = guild.get_member(int(member_id))
            if member is None:
                continue  # they're not in the server (anymore/yet) — nothing to restore for them
            roles_to_add = [role_map[name] for name in role_names if name in role_map]
            if roles_to_add:
                try:
                    await member.add_roles(*roles_to_add, reason=f"Server restore from backup '{backup_name}' (roles)")
                    restored_members += 1
                except discord.Forbidden:
                    pass

    summary_lines = []
    if restore_roles:
        summary_lines.append(f"Recreated {len(data['roles'])} roles.")
        summary_lines.append(f"Re-assigned roles to {restored_members} member(s) currently in this server.")
    if restore_channels:
        summary_lines.append(f"Recreated {len(data['categories'])} categories and {len(data['channels'])} channels (with permissions).")

    embed = discord.Embed(
        title="✅ Restore Complete",
        description="\n".join(summary_lines),
        color=discord.Color.green(),
    )
    await ctx.send(embed=embed)
    await mod_log(guild, "Server Restored From Backup", guild.me, ctx.author, f"Backup: {backup_name} ({what_label})", discord.Color.blue())


@bot.hybrid_command()
@backup_permission()
async def listbackups(ctx):
    """Lists backups. This server's own owner sees just THIS server's saves. You (the bot
    creator) see EVERY backup you've ever made across every server, and which one (if any)
    is currently auto-syncing in each."""
    if ctx.author.id == OWNER_ID:
        guild_folders = []
        if os.path.isdir(BACKUPS_FOLDER):
            guild_folders = sorted(d for d in os.listdir(BACKUPS_FOLDER) if os.path.isdir(os.path.join(BACKUPS_FOLDER, d)))

        embed = discord.Embed(title="💾 All Backups (every server)", color=discord.Color.blurple())
        total = 0
        for guild_id_str in guild_folders:
            folder = os.path.join(BACKUPS_FOLDER, guild_id_str)
            json_files = [f for f in os.listdir(folder) if f.endswith(".json")]
            if not json_files:
                continue
            if len(embed.fields) >= 25:
                break  # Discord embeds cap out at 25 fields — extremely unlikely to hit this
            guild_obj = bot.get_guild(int(guild_id_str))
            guild_label = guild_obj.name if guild_obj else f"Unknown/left server (`{guild_id_str}`)"
            auto_name = guild_settings.get(guild_id_str, {}).get("auto_backup_name")

            lines = []
            for filename in sorted(json_files):
                normalized = filename[:-5]
                data = load_json(os.path.join(folder, filename))
                display = data.get("display_name", normalized)
                synced = " 🔄" if auto_name == normalized else ""
                lines.append(f"- `{display}`{synced}")
                total += 1
            embed.add_field(name=guild_label, value="\n".join(lines), inline=False)

        if total == 0:
            await ctx.send(embed=discord.Embed(description="No backups saved anywhere yet.", color=discord.Color.greyple()))
            return
        embed.set_footer(text=f"{total} backup(s) across {len(embed.fields)} server(s). 🔄 = currently auto-syncing")
        await ctx.send(embed=embed)
        return

    # This server's own owner: just this server's saves, same as before.
    files = list_backup_names(ctx.guild.id)
    if not files:
        await ctx.send(embed=discord.Embed(description="No backups saved yet.", color=discord.Color.greyple()))
        return
    auto_name = guild_settings.get(str(ctx.guild.id), {}).get("auto_backup_name")
    lines = []
    for f in files:
        synced = " 🔄" if auto_name == normalize_backup_name(f) else ""
        lines.append(f"- `{f}`{synced}")
    embed = discord.Embed(title="💾 Saved Backups", description="\n".join(lines), color=discord.Color.blurple())
    embed.set_footer(text="🔄 = currently auto-syncing")
    await ctx.send(embed=embed)


# ============================================================
# DATA RESILIENCE — export / import / auto-backup
# ------------------------------------------------------------
# Some hosts (Orihost included) wipe a bot's files on restart, redeploy, or "scheduled
# cleanup" — that's a host-storage behavior, not something fixable from inside the bot.
# The fix here is to keep a copy OFF the host: this section zips every save file (levels,
# birthdays, warnings/settings, reaction roles, starboard, and every server backup) and DMs
# it to you automatically on a schedule, plus lets you pull one manually or restore from one.
# ============================================================
DATA_FILES_FOR_EXPORT = [
    ROLES_FILE, LEVELS_FILE, REACTION_ROLES_FILE, AFK_FILE,
    GUILD_SETTINGS_FILE, BIRTHDAYS_FILE, GIVEAWAYS_FILE, STARBOARD_FILE,
]


def build_data_export_zip() -> io.BytesIO:
    """Zips every JSON data file plus the whole backups/ folder (server structure backups)
    into an in-memory file, ready to attach to a Discord message. Paths are stored relative
    to BASE_DIR so the zip extracts back into the right place regardless of machine/host."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in DATA_FILES_FOR_EXPORT:
            if os.path.exists(path):
                zf.write(path, arcname=os.path.relpath(path, BASE_DIR))
        for path in glob.glob(os.path.join(BACKUPS_FOLDER, "**", "*.json"), recursive=True):
            zf.write(path, arcname=os.path.relpath(path, BASE_DIR))
    buffer.seek(0)
    return buffer


def reload_all_data():
    """Re-reads every persisted data file from disk, IN PLACE — clearing and refilling the
    same dict objects every other part of the bot already holds a reference to, so an import
    takes effect immediately without needing a restart."""
    def refresh(target: dict, path: str):
        target.clear()
        target.update(load_json(path))

    refresh(stored_roles, ROLES_FILE)
    refresh(levels_data, LEVELS_FILE)
    refresh(reaction_roles, REACTION_ROLES_FILE)
    refresh(afk_data, AFK_FILE)
    refresh(guild_settings, GUILD_SETTINGS_FILE)
    refresh(birthdays_data, BIRTHDAYS_FILE)
    refresh(giveaways_data, GIVEAWAYS_FILE)
    refresh(starboard_data, STARBOARD_FILE)


@bot.hybrid_command()
@owner_only()
@discord.app_commands.describe(message="What to announce — sent as an embed in every server I'm currently in")
async def broadcast(ctx, *, message: str):
    """Bot-creator only. Posts an embed with your message into every server the bot is
    currently in (system channel if possible, otherwise the first channel it can talk in).
    Handy for things like maintenance-mode notices. Usage: !broadcast Going down for
    maintenance at 10pm EST, back in an hour."""
    embed = discord.Embed(title="📢 Announcement", description=message, color=discord.Color.gold())
    embed.set_footer(text=f"Message from {bot.user.name}'s creator")

    sent, failed = 0, []
    for guild in bot.guilds:
        channel = await find_greetable_channel(guild)
        if channel is None:
            failed.append(guild.name)
            continue
        try:
            await channel.send(embed=embed)
            sent += 1
        except discord.Forbidden:
            failed.append(guild.name)

    summary = discord.Embed(
        title="📢 Broadcast Sent",
        description=f"Delivered to **{sent}/{len(bot.guilds)}** server(s).",
        color=discord.Color.green() if not failed else discord.Color.orange(),
    )
    if failed:
        summary.add_field(name="⚠️ Couldn't reach", value=", ".join(failed)[:1024], inline=False)
    await ctx.send(embed=summary)


# ============================================================
# "ANNI" — bot-creator-only, permanent deletion commands. Named after "annihilate": these
# don't archive or soft-delete anything, they call Discord's real delete endpoints. There is
# no undo beyond restoring a backup made BEFORE running one of these (!backupserver /
# !restoreserver). !anniserver asks for typed confirmation first since it wipes everything.
# ============================================================
@bot.hybrid_command()
@commands.guild_only()
@owner_only()
@discord.app_commands.describe(channel="The channel to permanently delete")
async def annichannel(ctx, channel: discord.abc.GuildChannel):
    """Bot-creator only. Permanently deletes a single channel — gone, not archived.
    Usage: !annichannel #some-channel"""
    name = channel.name
    was_this_channel = getattr(ctx, "channel", None) and channel.id == ctx.channel.id
    guilds_in_bulk_delete.add(ctx.guild.id)
    try:
        await channel.delete(reason=f"Annihilated by bot owner ({ctx.author})")
    except discord.Forbidden:
        guilds_in_bulk_delete.discard(ctx.guild.id)
        await ctx.send(embed=discord.Embed(title="⚠️ Error — NO_PERMISSION", description="I don't have permission to delete that channel.", color=discord.Color.red()))
        return
    guilds_in_bulk_delete.discard(ctx.guild.id)

    summary = discord.Embed(description=f"💥 Deleted channel `#{name}`.", color=discord.Color.dark_red())
    if was_this_channel:
        await dm_owner(f"💥 Deleted channel `#{name}` in **{ctx.guild.name}** (it was the channel the command ran in).", color=discord.Color.dark_red())
    else:
        await ctx.send(embed=summary)


@bot.hybrid_command()
@commands.guild_only()
@owner_only()
@discord.app_commands.describe(category="The category to permanently delete, along with every channel inside it")
async def annicategory(ctx, category: discord.CategoryChannel):
    """Bot-creator only. Permanently deletes a category AND every channel inside it — gone,
    not archived. Usage: !annicategory CategoryName"""
    channels = list(category.channels)
    ran_inside_this_category = getattr(ctx, "channel", None) and getattr(ctx.channel, "category_id", None) == category.id

    guilds_in_bulk_delete.add(ctx.guild.id)
    deleted = 0
    for ch in channels:
        try:
            await ch.delete(reason=f"Annihilated by bot owner ({ctx.author}) — category wipe")
            deleted += 1
        except discord.HTTPException:
            pass
    try:
        await category.delete(reason=f"Annihilated by bot owner ({ctx.author})")
    except discord.Forbidden:
        guilds_in_bulk_delete.discard(ctx.guild.id)
        await ctx.send(embed=discord.Embed(title="⚠️ Error — NO_PERMISSION", description=f"Deleted {deleted}/{len(channels)} channel(s) inside, but couldn't delete the category itself.", color=discord.Color.red()))
        return
    guilds_in_bulk_delete.discard(ctx.guild.id)

    summary_text = f"💥 Deleted category `{category.name}` and {deleted} channel(s) inside it."
    if ran_inside_this_category:
        await dm_owner(f"{summary_text} (command ran inside that category, so this went to your DMs instead.)", color=discord.Color.dark_red())
    else:
        await ctx.send(embed=discord.Embed(description=summary_text, color=discord.Color.dark_red()))


@bot.hybrid_command()
@commands.guild_only()
@owner_only()
async def anniserver(ctx):
    """Bot-creator only. PERMANENTLY deletes EVERY channel, category, and custom role in
    THIS server — gone, not archived. Asks you to type `confirm` first since it cannot be
    undone (short of restoring a backup made beforehand). Usage: !anniserver"""
    guild = ctx.guild
    await ctx.send(embed=discord.Embed(
        title="⚠️ Are you absolutely sure?",
        description=f"This will permanently delete **every channel, category, and custom role** in **{guild.name}**. This cannot be undone.\n\nType `confirm` within 30 seconds to proceed, or anything else (or nothing) to cancel.",
        color=discord.Color.red(),
    ))

    def check(m):
        return m.author.id == ctx.author.id and m.channel.id == ctx.channel.id

    try:
        reply = await bot.wait_for("message", check=check, timeout=30)
    except asyncio.TimeoutError:
        await ctx.send(embed=discord.Embed(description="⏱️ Timed out — nothing was deleted.", color=discord.Color.greyple()))
        return
    if reply.content.strip().lower() != "confirm":
        await ctx.send(embed=discord.Embed(description="Cancelled — nothing was deleted.", color=discord.Color.greyple()))
        return

    guilds_in_bulk_delete.add(guild.id)
    deleted_channels = 0
    for channel in list(guild.channels):
        try:
            await channel.delete(reason=f"Server annihilated by bot owner ({ctx.author})")
            deleted_channels += 1
        except discord.HTTPException:
            pass

    deleted_roles = 0
    for role in list(guild.roles):
        if role.is_default() or role.managed:
            continue
        try:
            await role.delete(reason=f"Server annihilated by bot owner ({ctx.author})")
            deleted_roles += 1
        except discord.HTTPException:
            pass
    guilds_in_bulk_delete.discard(guild.id)

    # The channel this command ran in was very likely just deleted above, so report the
    # result over DM instead of trying (and probably failing) to send it back there.
    await dm_owner(f"💥 Annihilated **{guild.name}** — deleted {deleted_channels} channel(s) and {deleted_roles} role(s).", color=discord.Color.dark_red())


@bot.hybrid_command()
@owner_only()
async def exportdata(ctx):
    """DMs you a zip of every save file — levels, birthdays, settings, reaction roles,
    starboard, and every server backup — so you have a copy that survives a host wipe.
    Usage: !exportdata"""
    buffer = build_data_export_zip()
    filename = f"backup_export_{datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d_%H%M')}.zip"
    try:
        owner = await bot.fetch_user(OWNER_ID)
        await owner.send(embed=discord.Embed(description="📦 Here's your full data export.", color=discord.Color.blurple()), file=discord.File(buffer, filename=filename))
        if ctx.guild is not None:
            await ctx.send(embed=discord.Embed(description="📦 Sent you a DM with the full backup zip.", color=discord.Color.green()))
    except discord.Forbidden:
        await ctx.send(embed=discord.Embed(title="⚠️ Error — DM_CLOSED", description="Couldn't DM you the export — your DMs are closed. Open your DMs to me, or run this command in a DM with me instead.", color=discord.Color.red()))


@bot.hybrid_command()
@owner_only()
async def importdata(ctx, archive: discord.Attachment):
    """Restores every save file from a zip made by !exportdata (or an auto-backup DM).
    Attach the .zip to this command. THIS OVERWRITES your current data files — only run
    this to recover after a wipe. Usage: !importdata (with the zip attached)"""
    if not archive.filename.lower().endswith(".zip"):
        await ctx.send(embed=discord.Embed(description="That doesn't look like a `.zip` export — attach the file `!exportdata` (or the auto-backup) sent you.", color=discord.Color.red()))
        return

    raw = await archive.read()
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            zf.extractall(BASE_DIR)
    except zipfile.BadZipFile:
        await ctx.send(embed=discord.Embed(description="⚠️ That zip file looks corrupted — try re-exporting or use an older backup DM.", color=discord.Color.red()))
        return

    reload_all_data()
    await ctx.send(embed=discord.Embed(
        title="✅ Data Restored",
        description="Levels, birthdays, settings, reaction roles, starboard, and server backups have all been restored from that export and reloaded — no restart needed.",
        color=discord.Color.green(),
    ))


@tasks.loop(hours=6)
async def auto_data_backup():
    """Every 6 hours, automatically DMs you a fresh data export — so even if you never run
    !exportdata yourself, you've always got a recent off-host copy waiting in your DMs if
    the host wipes the disk. Purely a safety net; costs nothing to leave running."""
    try:
        buffer = build_data_export_zip()
        filename = f"auto_backup_{datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d_%H%M')}.zip"
        owner = await bot.fetch_user(OWNER_ID)
        await owner.send(embed=discord.Embed(description="📦 Automatic data backup (every 6h) — keep this around in case the host wipes my files.", color=discord.Color.blurple()), file=discord.File(buffer, filename=filename))
    except Exception as e:
        print(f"⚠️ Auto data backup failed: {e}")


@auto_data_backup.before_loop
async def before_auto_data_backup():
    await bot.wait_until_ready()


bot.run(TOKEN)
