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
import unicodedata
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
bot.help_command = None  # replaced by our own button-based !help command further down

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


# Leetspeak/lookalike substitutes tolerated per letter when matching banned words below —
# e.g. the letter 'g' also matches a literal '6' or '9' (both common g-substitutes).
LEET_LETTER_MAP = {
    "a": "a4@", "b": "b8", "e": "e3", "g": "g69", "i": "i1!",
    "l": "l1", "o": "o0", "s": "s5$", "t": "t7", "z": "z2",
}
_banned_word_pattern_cache = {}  # word (lowercase) -> compiled regex, built lazily and reused


def build_banned_word_pattern(word: str) -> re.Pattern:
    """Builds a whole-word regex for one banned word that tolerates common bypass tricks —
    leetspeak substitutions (1/i, 9/g, etc), stretched-out repeated letters ('niiiggger'),
    spaced/punctuated-out letters ('n i g g e r'), and a trailing plural 's'/'es' — while
    still respecting word boundaries, so a short banned word never matches INSIDE an
    unrelated longer word (e.g. banning 'pred' won't catch 'predict' — the boundary check
    requires 'pred' to end the word there, not continue into more letters)."""
    parts = []
    for ch in word.lower():
        if ch.isalpha():
            variants = LEET_LETTER_MAP.get(ch, ch)
            parts.append(f"[{re.escape(variants)}]+")
        else:
            parts.append(re.escape(ch) + "+")
    pattern = r"\b" + r"[\W_]*".join(parts) + r"(?:e?s)?\b"
    return re.compile(pattern, re.IGNORECASE)


def get_banned_word_pattern(word: str) -> re.Pattern:
    if word not in _banned_word_pattern_cache:
        _banned_word_pattern_cache[word] = build_banned_word_pattern(word)
    return _banned_word_pattern_cache[word]


def strip_unicode_lookalikes(text: str) -> str:
    """Decomposes accented letters and Discord 'fancy font' Unicode tricks (bold/italic/
    script/fraktur/circled/fullwidth — e.g. '𝓷𝓲𝓰𝓰𝓮𝓻' or 'nïgger') down to plain letters
    before banned-word matching, using Unicode's own compatibility-decomposition data.
    Note: this does NOT catch cross-script lookalikes (e.g. a Cyrillic 'і' standing in for
    Latin 'i') — that's a different, harder problem than font styling."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def contains_banned_word(text: str, guild_id=None) -> bool:
    text = strip_unicode_lookalikes(text)
    words = BAD_WORDS
    if guild_id is not None:
        custom = guild_settings.get(str(guild_id), {}).get("banned_words", [])
        if custom:
            words = BAD_WORDS + custom
    return any(get_banned_word_pattern(word).search(text) for word in words)


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
MAX_LEVEL = 100000  # sanity cap — !setlevel/!addxp can't push anyone past this. Purely a
                     # safety net: nothing legitimate needs a level this high, and it keeps
                     # a typo'd extra zero or two from producing a genuinely absurd value.
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
INVITES_FILE = os.path.join(BASE_DIR, "invites.json")
GLOBAL_BANS_FILE = os.path.join(BASE_DIR, "global_bans.json")
ACTIVITY_FILE = os.path.join(BASE_DIR, "activity.json")
ROLE_MENUS_FILE = os.path.join(BASE_DIR, "role_menus.json")


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
# invites.json format, per server:
# {"guild_id": {"invite_counts": {"inviter_user_id": total_successful_invites},
#                "joins": {"member_user_id": {"inviter_id", "invite_code", "since"}}}}
invite_data = load_json(INVITES_FILE)
# global_bans.json format: {"user_id": {"reason", "banned_by", "timestamp"}} — anyone here
# gets banned from every server the bot is in, and auto-banned in any server it joins later
global_bans_data = load_json(GLOBAL_BANS_FILE)
# activity.json format: {"guild_id": {"user_id": {"daily": {"YYYY-MM-DD": {"messages": int,
# "voice_seconds": float}}}}} — day-bucketed so "most active" can mean genuinely/recently
# active (a rolling window, see ACTIVE_WINDOW_DEFAULT_DAYS) rather than an all-time total
# someone could win once and hold forever. Tracked separately from XP/levels so it still
# works even if a server has leveling turned off. Old buckets get pruned periodically.
activity_data = load_json(ACTIVITY_FILE)
# role_menus.json format: {"message_id": {"guild_id", "roles": [role_id,...],
# "single_choice": bool, "removable": bool}} — only needed for menus using single_choice or
# non-removable; a plain default-behavior menu's buttons are still fully self-contained.
role_menus_data = load_json(ROLE_MENUS_FILE)
guild_invite_cache = {}  # runtime only: guild_id -> {invite_code: uses} snapshot, used to spot
                          # which invite's use-count went up when someone joins


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
    if not server_stats_and_active_roles_update.is_running():
        server_stats_and_active_roles_update.start()
    if not timeout_expiry_check.is_running():
        timeout_expiry_check.start()
    if not voice_reconnect_check.is_running():
        voice_reconnect_check.start()


async def cache_guild_invites(guild: discord.Guild):
    """Snapshots this server's current invites (code -> uses) so a future join can be
    matched to whichever invite's use-count went up. Needs Manage Server permission —
    silently gives up (no invite tracking there) if the bot doesn't have it."""
    try:
        invites = await guild.invites()
        guild_invite_cache[guild.id] = {invite.code: (invite.uses or 0) for invite in invites}
    except discord.Forbidden:
        guild_invite_cache[guild.id] = {}


async def detect_inviter(guild: discord.Guild):
    """Compares this server's invites against the last cached snapshot to find whichever
    invite's use-count just went up — that invite's creator is who invited the newest member.
    Returns (inviter_member_or_user, invite_code), or (None, None) if it can't be determined
    (vanity URL, widget invite, permission issue, or a race with another simultaneous join)."""
    try:
        current_invites = await guild.invites()
    except discord.Forbidden:
        return None, None

    old_cache = guild_invite_cache.get(guild.id, {})
    inviter, invite_code = None, None
    for inv in current_invites:
        if (inv.uses or 0) > old_cache.get(inv.code, 0):
            inviter, invite_code = inv.inviter, inv.code
            break

    guild_invite_cache[guild.id] = {inv.code: (inv.uses or 0) for inv in current_invites}
    return inviter, invite_code


@bot.event
async def on_invite_create(invite):
    cache = guild_invite_cache.setdefault(invite.guild.id, {})
    cache[invite.code] = invite.uses or 0


@bot.event
async def on_invite_delete(invite):
    guild_invite_cache.setdefault(invite.guild.id, {}).pop(invite.code, None)


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

    for guild in bot.guilds:
        await cache_guild_invites(guild)

    await dm_owner(f"✅ **{bot.user.name}** just came online.")


HELP_CATEGORIES = {
    "setup": {
        "label": "⚙️ Setup",
        "title": "⚙️ Setup Commands",
        "description": "Needs Manage Server permission, or the bot owner.",
        "commands": (
            "`setwelcomechannel` `setgoodbyechannel` `setmodlogchannel` `settimeoutchannel` `settimeoutrole`\n"
            "`setlevelupchannel` `setbirthdaychannel` `setstarboardchannel` `setstarboardthreshold`\n"
            "`setannouncementchannel` `setxpamount` `setvoicexpamount` `togglelevels`\n"
            "`setlevelrole` `removelevelrole` `listlevelroles` `addbannedword` `removebannedword`\n"
            "`bannedwords` `showsettings`\n"
            "`setmemberrole` `setbotrole` — auto-role new humans/bots on join\n"
            "`setupserverstats` `removeserverstats` — live member/human/bot count channels\n"
            "`setactivechatrole` `setactivevoicerole` `setactiveoverallrole` — auto-role for EVERYONE who's genuinely active weekly\n"
            "`setyapperrole` `setgrandyapperrole` — single-holder \"most messages today/this week\" roles\n"
            "`setactivitywindow` — how many recent days count toward that"
        ),
    },
    "moderation": {
        "label": "🛡️ Moderation",
        "title": "🛡️ Moderation Commands",
        "description": "Needs the matching Discord permission (kick/ban/manage channels), or the bot owner.",
        "commands": (
            "`kick` `ban` `timeout` `untimeout` `clear` `clearuser` `clearkeyword`\n"
            "`lockdown` `unlock` `lockchannel` `unlockchannel`"
        ),
    },
    "leveling": {
        "label": "📈 Leveling & Roles",
        "title": "📈 Leveling, Activity & Role Menus",
        "description": "XP progress, level-up role rewards, activity leaderboards, and self-role menus.",
        "commands": (
            "`rank` `leaderboard` `globalleaderboard` `setlevel` `addxp`\n"
            "`activeleaderboard` — chat/voice, today/weekly/all-time (dropdown to switch)\n"
            "`rolemenu` — button-based self-roles (recommended!)\n"
            "`reactionrole` `createreactionrole` `removereactionrole` — older, reaction-based"
        ),
    },
    "fun": {
        "label": "🎉 Fun & Extras",
        "title": "🎉 Fun & Extras",
        "description": "Just for fun.",
        "commands": (
            "`8ball` `coinflip` `roll` `rps` `joke`\n"
            "`afk` `birthday` `setbirthday` `giveaway` `gend` `greroll` `giveawayentrants`\n"
            "`togglegiveawayblacklist` `setgiveawaybonusrole` `setgiveawaybonusmember` `togglegiveawaydailyentries`"
        ),
    },
    "backups": {
        "label": "💾 Backups",
        "title": "💾 Server Backups",
        "description": "Save and restore a server's roles/channels.",
        "commands": (
            "`backupserver <name>` — saves roles/channels and keeps auto-syncing them\n"
            "`restoreserver <name> [all|roles|channels]` `listbackups`\n"
            "`autobackup` (bot owner only)"
        ),
    },
    "owner": {
        "label": "👑 Owner-Only",
        "title": "👑 Bot-Creator-Only Commands",
        "description": "Only the bot's creator can run these — powerful, cross-server, or destructive.",
        "commands": (
            "`broadcast` — message every server at once\n"
            "`globalban` `globalunban` `globalbanlist` — bans across every server\n"
            "`annirole` `annichannel` `annicategory` `anniserver` — permanent deletion\n"
            "`exportdata` `importdata` — off-host data backup/restore\n"
            "`joinvc` `leavevc` — voice channel presence\n"
            "`invites` `inviteleaderboard` — invite tracking"
        ),
    },
}


def build_help_embed(category: str) -> discord.Embed:
    """Builds the embed for one !help panel — 'home' is the landing page, anything else is
    a HELP_CATEGORIES key."""
    if category == "home":
        embed = discord.Embed(
            title=f"📖 {bot.user.name} — Command Help",
            description=(
                "Click a category below to see its commands. Prefix commands use `!`, or use "
                "any of these as `/slash` commands too.\n\n"
                "**Before anything else:** make sure the bot owner stays a member of this "
                "server with Administrator — most features (backups, moderation, channel "
                "setup) need it to work properly."
            ),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="Type !showsettings any time to see this server's current configuration.")
        return embed

    info = HELP_CATEGORIES[category]
    return discord.Embed(title=info["title"], description=f"{info['description']}\n\n{info['commands']}", color=discord.Color.blurple())


class HelpView(discord.ui.View):
    """Category buttons for !help — clicking one edits the embed in place instead of dumping
    every command as one wall of text."""
    def __init__(self, is_owner: bool, timeout: float = 180):
        super().__init__(timeout=timeout)
        row0_categories = ["setup", "moderation", "leveling", "fun", "backups"]
        for key in row0_categories:
            info = HELP_CATEGORIES[key]
            button = discord.ui.Button(label=info["label"], style=discord.ButtonStyle.primary, row=0)
            button.callback = self._make_callback(key)
            self.add_item(button)

        if is_owner:
            owner_info = HELP_CATEGORIES["owner"]
            owner_button = discord.ui.Button(label=owner_info["label"], style=discord.ButtonStyle.danger, row=1)
            owner_button.callback = self._make_callback("owner")
            self.add_item(owner_button)

        home_button = discord.ui.Button(label="🏠 Home", style=discord.ButtonStyle.secondary, row=1)
        home_button.callback = self._make_callback("home")
        self.add_item(home_button)

    def _make_callback(self, category: str):
        async def callback(interaction: discord.Interaction):
            await interaction.response.edit_message(embed=build_help_embed(category))
        return callback


@bot.hybrid_command(name="help")
async def help_command(ctx):
    """Shows an interactive button menu of every command, grouped by category. Usage: !help"""
    is_owner = ctx.author.id == OWNER_ID
    await ctx.send(embed=build_help_embed("home"), view=HelpView(is_owner=is_owner))


async def find_greetable_channel(guild: discord.Guild):
    """Picks the best channel to post a one-time message in: the system channel if the bot
    can talk there, otherwise the first text channel it can."""
    if guild.system_channel and guild.system_channel.permissions_for(guild.me).send_messages:
        return guild.system_channel
    return next((c for c in guild.text_channels if c.permissions_for(guild.me).send_messages), None)


async def get_announcement_channel(guild: discord.Guild):
    """Where bot-wide announcements (broadcasts, the join setup-guide) post in this server.
    Uses the channel set with !setannouncementchannel if there is one and the bot can still
    post there; otherwise falls back to find_greetable_channel's default (system channel,
    or the first channel the bot can talk in)."""
    configured = get_guild_channel(guild.id, "announcement_channel")
    if configured and configured.permissions_for(guild.me).send_messages:
        return configured
    return await find_greetable_channel(guild)


@bot.event
async def on_guild_join(guild):
    """Fires the instant someone adds the bot to a new server."""
    if REQUIRE_OWNER_PRESENT and not await owner_in_guild(guild):
        await handle_unauthorized_guild(guild)
        return

    await dm_owner(f"➕ Added to a new server: **{guild.name}** (`{guild.id}`).")
    await cache_guild_invites(guild)

    if global_bans_data:
        banned_here = 0
        for user_id_str in list(global_bans_data.keys()):
            member = guild.get_member(int(user_id_str))
            if member is None:
                continue
            try:
                await member.ban(reason=f"GLOBAL BAN (auto-enforced on join): {global_bans_data[user_id_str].get('reason', 'No reason given')}")
                banned_here += 1
            except discord.Forbidden:
                pass
        if banned_here:
            await dm_owner(f"🌐 Auto-banned {banned_here} globally-banned member(s) already present in **{guild.name}**.")

    channel = await get_announcement_channel(guild)
    if channel:
        try:
            await channel.send(
                embed=discord.Embed(
                    title=f"👋 Thanks for adding {bot.user.name}!",
                    description="Run `/help` any time for an interactive menu of every command, grouped by category.",
                    color=discord.Color.blurple(),
                ),
                view=HelpView(is_owner=False),
            )
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

    if str(member.id) in global_bans_data:
        reason = global_bans_data[str(member.id)].get("reason", "No reason given")
        try:
            await member.ban(reason=f"GLOBAL BAN (auto-enforced): {reason}")
            await dm_owner(f"🌐 Auto-banned **{member}** in **{member.guild.name}** — they're on the global ban list ({reason}).")
        except discord.Forbidden:
            await dm_owner(f"⚠️ {member} is globally banned but I couldn't auto-ban them in **{member.guild.name}** — missing permission.")
        return  # don't welcome/track invites for someone who was just banned

    # Auto-role: a different role for humans vs bots, set with !setmemberrole / !setbotrole
    settings = guild_settings.get(str(member.guild.id), {})
    auto_role_id = settings.get("auto_bot_role") if member.bot else settings.get("auto_member_role")
    if auto_role_id:
        auto_role = member.guild.get_role(auto_role_id)
        if auto_role:
            try:
                await member.add_roles(auto_role, reason="Auto-role on join")
            except discord.Forbidden:
                await dm_owner(f"⚠️ Tried to auto-give {member} the '{auto_role.name}' role in {member.guild.name} but don't have permission.")

    channel = get_guild_channel(member.guild.id, "welcome_channel")
    if channel:
        embed = discord.Embed(
            title="Welcome! 🎉",
            description=f"Hey {member.mention}, glad you're here! You're member #{member.guild.member_count}.",
            color=discord.Color.green(),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        await channel.send(embed=embed)

    inviter, invite_code = await detect_inviter(member.guild)
    if inviter:
        guild_data = invite_data.setdefault(str(member.guild.id), {"invite_counts": {}, "joins": {}})
        guild_data["invite_counts"][str(inviter.id)] = guild_data["invite_counts"].get(str(inviter.id), 0) + 1
        guild_data["joins"][str(member.id)] = {
            "inviter_id": inviter.id,
            "invite_code": invite_code,
            "since": datetime.datetime.now(datetime.timezone.utc).timestamp(),
        }
        save_json(INVITES_FILE, invite_data)
        await dm_owner(f"➕ **{member}** joined **{member.guild.name}**, invited by **{inviter}** (now {guild_data['invite_counts'][str(inviter.id)]} invite(s) there).")
    else:
        await dm_owner(f"➕ **{member}** joined **{member.guild.name}** — couldn't tell who invited them (could be a vanity URL, server widget, or I'm missing Manage Server permission there).")

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


@bot.event
async def on_interaction(interaction: discord.Interaction):
    """Raw handler for the persistent role-menu and giveaway-enter buttons. Deliberately NOT
    built on discord.py's View/callback system — a plain button with just a custom_id is
    enough, since everything it needs (the role ID, or the giveaway's message ID) is encoded
    directly in that custom_id. That means these buttons keep working forever, even across
    bot restarts, with no re-registration step needed on startup."""
    if interaction.type != discord.InteractionType.component:
        return
    custom_id = interaction.data.get("custom_id", "") if interaction.data else ""

    if custom_id.startswith("rolemenu:"):
        parts = custom_id.split(":")
        if len(parts) == 2:
            # Old format from before menus had metadata: rolemenu:role_id — plain toggle,
            # no single-choice/non-removable rules apply.
            role_id = int(parts[1])
            menu = None
        else:
            # New format: rolemenu:message_id:role_id
            role_id = int(parts[2])
            menu = role_menus_data.get(parts[1])

        role = interaction.guild.get_role(role_id) if interaction.guild else None
        if role is None:
            await interaction.response.send_message("That role doesn't exist anymore.", ephemeral=True)
            return
        member = interaction.user
        single_choice = menu.get("single_choice", False) if menu else False
        removable = menu.get("removable", True) if menu else True
        menu_role_ids = set(menu.get("roles", [])) if menu else set()

        try:
            if role in member.roles:
                if not removable:
                    await interaction.response.send_message(f"You already have **{role.name}** — this menu doesn't allow removing roles once picked.", ephemeral=True)
                    return
                await member.remove_roles(role, reason="Role menu button")
                await interaction.response.send_message(f"➖ Removed **{role.name}**.", ephemeral=True)
            else:
                if single_choice:
                    other_held = [r for r in member.roles if r.id in menu_role_ids and r.id != role_id]
                    if other_held:
                        await member.remove_roles(*other_held, reason="Role menu button (single-choice swap)")
                await member.add_roles(role, reason="Role menu button")
                await interaction.response.send_message(f"➕ Gave you **{role.name}**.", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("I don't have permission to manage that role — check my role's position in the server's role list (I need to be above it).", ephemeral=True)

    elif custom_id.startswith("giveaway_enter:"):
        message_id = custom_id.split(":", 1)[1]
        data = giveaways_data.get(message_id)
        if data is None:
            await interaction.response.send_message("This giveaway has ended.", ephemeral=True)
            return

        member = interaction.user
        guild_id_str = str(interaction.guild.id) if interaction.guild else None
        settings = guild_settings.get(guild_id_str, {}) if guild_id_str else {}
        blacklist = set(settings.get("giveaway_blacklist_roles", []))
        if any(r.id in blacklist for r in getattr(member, "roles", [])):
            await interaction.response.send_message("🚫 You're not allowed to enter this giveaway.", ephemeral=True)
            return

        entrants = data.setdefault("entrants", {})
        if isinstance(entrants, list):  # tolerate the old plain-list format from before entry counts existed
            entrants = {str(uid): {"count": 1} for uid in entrants}
            data["entrants"] = entrants

        user_id_str = str(member.id)
        today = _today_key()
        record = entrants.get(user_id_str)
        daily_entries_on = settings.get("giveaway_daily_entries", False)

        if record is None:
            entrants[user_id_str] = {"count": 1, "last_entry_day": today}
            await interaction.response.send_message("🎉 You're entered! Good luck!", ephemeral=True)
        elif daily_entries_on and record.get("last_entry_day") != today:
            record["count"] = record.get("count", 1) + 1
            record["last_entry_day"] = today
            await interaction.response.send_message(f"🎉 Extra entry added! You now have **{record['count']}** entries.", ephemeral=True)
        else:
            del entrants[user_id_str]
            await interaction.response.send_message("➖ You left the giveaway.", ephemeral=True)
        save_json(GIVEAWAYS_FILE, giveaways_data)


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


def parse_message_id(text: str):
    """Accepts either a raw message ID or a full 'Copy Message Link' URL and returns the
    message ID as an int, or None if nothing usable was found. Using str here (not int) for
    the command parameter matters: a slash command parameter typed as an integer gets
    rejected client-side by Discord itself the instant someone pastes a link or anything
    non-numeric, before it even reaches the bot — accepting text and parsing it ourselves
    avoids that entirely."""
    match = re.search(r"(\d{15,25})\s*$", text.strip())
    return int(match.group(1)) if match else None


@bot.hybrid_command()
@has_permissions_or_owner(manage_roles=True)
@discord.app_commands.describe(message_id="The message's ID, or its 'Copy Message Link' URL", emoji="The emoji to react with", role="Role to give when someone reacts with that emoji")
async def reactionrole(ctx, message_id: str, emoji: str, role: discord.Role):
    """Link an emoji on a message to a role. Usage: !reactionrole <message_id or link> <emoji> @Role
    This is now saved permanently — it survives bot restarts."""
    parsed_id = parse_message_id(message_id)
    if parsed_id is None:
        await ctx.send(embed=discord.Embed(description="Couldn't read a message ID out of that — paste the raw ID (enable Developer Mode, right-click the message → Copy Message ID) or the full 'Copy Message Link'.", color=discord.Color.red()))
        return
    reaction_roles.setdefault(str(parsed_id), {})[emoji] = role.id
    save_json(REACTION_ROLES_FILE, reaction_roles)
    await ctx.send(embed=discord.Embed(description=f"✅ Linked {emoji} on message `{parsed_id}` to **{role.name}** — saved permanently.", color=discord.Color.green()))


@bot.hybrid_command()
@has_permissions_or_owner(manage_roles=True)
@discord.app_commands.describe(message_id="The message's ID, or its 'Copy Message Link' URL", emoji="The emoji pairing to remove")
async def removereactionrole(ctx, message_id: str, emoji: str):
    """Remove a reaction role pairing. Usage: !removereactionrole <message_id or link> <emoji>"""
    parsed_id = parse_message_id(message_id)
    if parsed_id is None:
        await ctx.send(embed=discord.Embed(description="Couldn't read a message ID out of that — paste the raw ID or the full message link.", color=discord.Color.red()))
        return
    if str(parsed_id) in reaction_roles and emoji in reaction_roles[str(parsed_id)]:
        del reaction_roles[str(parsed_id)][emoji]
        save_json(REACTION_ROLES_FILE, reaction_roles)
        await ctx.send(embed=discord.Embed(description=f"🗑️ Removed {emoji} pairing from message `{parsed_id}`.", color=discord.Color.orange()))
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
@has_permissions_or_owner(manage_roles=True)
@discord.app_commands.describe(
    title="Title shown at the top of the menu",
    role1="First role", role2="Second role (optional)", role3="Third role (optional)",
    role4="Fourth role (optional)", role5="Fifth role (optional)",
    description="Optional text under the title (defaults to a simple instruction)",
    image="Optional image URL — shown as a big banner at the bottom of the menu",
    thumbnail="Optional image URL — shown as a small thumbnail in the top-right corner",
    color="Optional hex color for the embed's side bar, e.g. #5865F2",
    single_choice="If true, picking a role removes any other role from THIS menu — only one at a time",
    removable="If false, members can't remove a role once they've picked it from this menu (default true)",
)
async def rolemenu(ctx, title: str, role1: discord.Role, role2: discord.Role = None,
                    role3: discord.Role = None, role4: discord.Role = None, role5: discord.Role = None,
                    description: str = None, image: str = None, thumbnail: str = None, color: str = None,
                    single_choice: bool = False, removable: bool = True):
    """Posts a button-based self-role menu — click a button to get that role, click it again
    to remove it. No reactions involved, and it keeps working after a bot restart. Add an
    image/thumbnail/color to make it match your server's vibe. Set single_choice=True for a
    "pick only one" menu (choosing a new one swaps out the old), or removable=False so a
    role can't be removed via this menu once picked.
    Usage: !rolemenu "Pick your pings" @VC @Announcements @Events @Giveaways
    Usable by anyone with Manage Roles (Administrators included) or the bot owner."""
    roles = [r for r in (role1, role2, role3, role4, role5) if r is not None]
    embed_color = parse_hex_color(color) or discord.Color.blurple()

    description_text = description or "Click a button below to toggle a role."
    if single_choice:
        description_text += "\n*You can only hold one role from this menu at a time.*"
    if not removable:
        description_text += "\n*Roles picked here can't be removed through this menu.*"

    embed = discord.Embed(title=title, description=description_text, color=embed_color)
    if image:
        embed.set_image(url=image)
    if thumbnail:
        embed.set_thumbnail(url=thumbnail)

    view = discord.ui.View(timeout=None)
    for role in roles:
        view.add_item(discord.ui.Button(label=role.name[:80], style=discord.ButtonStyle.secondary, custom_id=f"rolemenu:pending:{role.id}"))
    message = await ctx.send(embed=embed, view=view)

    # Now that we have the real message ID, bake it into every button's custom_id, and
    # persist the menu's metadata — needed so the single_choice/removable rules can be
    # enforced (they require knowing every role in the menu, not just the one clicked).
    for item in view.children:
        role_id = item.custom_id.split(":")[2]
        item.custom_id = f"rolemenu:{message.id}:{role_id}"
    await message.edit(view=view)

    role_menus_data[str(message.id)] = {
        "guild_id": ctx.guild.id,
        "roles": [r.id for r in roles],
        "single_choice": single_choice,
        "removable": removable,
    }
    save_json(ROLE_MENUS_FILE, role_menus_data)


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
@discord.app_commands.describe(role="Role to auto-give every new HUMAN member (leave blank to turn this off)")
async def setmemberrole(ctx, role: discord.Role = None):
    """Auto-gives a role to every human who joins THIS server. Usage: !setmemberrole @Member
    (run with no role to turn it off)."""
    settings = guild_settings.setdefault(str(ctx.guild.id), {})
    if role is None:
        settings.pop("auto_member_role", None)
        save_json(GUILD_SETTINGS_FILE, guild_settings)
        await ctx.send(embed=discord.Embed(description="🛑 Turned off auto-role for new members.", color=discord.Color.orange()))
        return
    settings["auto_member_role"] = role.id
    save_json(GUILD_SETTINGS_FILE, guild_settings)
    await ctx.send(embed=discord.Embed(description=f"✅ New human members will now automatically get {role.mention}.", color=discord.Color.green()))


@bot.hybrid_command()
@commands.guild_only()
@has_permissions_or_owner(manage_guild=True)
@discord.app_commands.describe(role="Role to auto-give every new BOT added to the server (leave blank to turn this off)")
async def setbotrole(ctx, role: discord.Role = None):
    """Auto-gives a role to every bot added to THIS server. Usage: !setbotrole @Bots
    (run with no role to turn it off)."""
    settings = guild_settings.setdefault(str(ctx.guild.id), {})
    if role is None:
        settings.pop("auto_bot_role", None)
        save_json(GUILD_SETTINGS_FILE, guild_settings)
        await ctx.send(embed=discord.Embed(description="🛑 Turned off auto-role for new bots.", color=discord.Color.orange()))
        return
    settings["auto_bot_role"] = role.id
    save_json(GUILD_SETTINGS_FILE, guild_settings)
    await ctx.send(embed=discord.Embed(description=f"✅ New bots will now automatically get {role.mention}.", color=discord.Color.green()))


@bot.hybrid_command()
@commands.guild_only()
@has_permissions_or_owner(manage_guild=True)
async def setannouncementchannel(ctx, channel: discord.TextChannel):
    """Sets THIS server's channel for bot-wide announcements — right now that's the
    bot-creator's !broadcast messages, and the one-time setup guide posted when the bot
    joins. Without this set, those default to the system channel (often #general or
    whatever ends up being #rules-adjacent), which usually isn't ideal.
    Usage: !setannouncementchannel #announcements"""
    set_guild_channel(ctx.guild.id, "announcement_channel", channel.id)
    await ctx.send(embed=discord.Embed(description=f"📢 Bot announcements (including broadcasts) will now post in {channel.mention}.", color=discord.Color.green()))


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
    timeout_role = get_timeout_role(ctx.guild)
    if timeout_role:
        # A timeout role already exists — without this, changing the timeout channel later
        # wouldn't actually move the "visible but silent" exception to the new channel.
        await setup_timeout_role_permissions(ctx.guild, timeout_role)
    await ctx.send(embed=discord.Embed(description=f"✅ Timed-out members will now only see {channel.mention}.", color=discord.Color.green()))


@bot.hybrid_command()
@commands.guild_only()
@has_permissions_or_owner(manage_roles=True)
@discord.app_commands.describe(role="The role to use for timeouts (leave blank to go back to auto-creating/using a role named 'Timed Out')")
async def settimeoutrole(ctx, role: discord.Role = None):
    """Sets which role THIS server's timeout system uses, instead of the bot auto-creating
    one called 'Timed Out'. Automatically (re)configures that role's channel permissions —
    hidden everywhere except the timeout channel (if set with !settimeoutchannel), same as
    the auto-created role would get. Usage: !settimeoutrole @Muted"""
    settings = guild_settings.setdefault(str(ctx.guild.id), {})
    if role is None:
        settings.pop("timeout_role_id", None)
        save_json(GUILD_SETTINGS_FILE, guild_settings)
        await ctx.send(embed=discord.Embed(description="🛑 Back to auto-creating/using a role named 'Timed Out'.", color=discord.Color.orange()))
        return
    settings["timeout_role_id"] = role.id
    save_json(GUILD_SETTINGS_FILE, guild_settings)
    await setup_timeout_role_permissions(ctx.guild, role)
    await ctx.send(embed=discord.Embed(description=f"✅ Timeouts will now use {role.mention} — its channel permissions have been set up automatically.", color=discord.Color.green()))


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
    """Sets a role to be auto-given once someone reaches OR passes this level, IN THIS SERVER
    ONLY. It's a threshold, not an exact match — jumping straight past it (e.g. a big XP grant)
    still earns it. Works as a ladder: once someone qualifies for a HIGHER level-role, their
    previous tier's role is taken back, so they only ever hold their current one. Setting this
    retroactively updates everyone who already qualifies, not just future level-ups. Usage:
    !setlevelrole 15 @Wizard. Usable by anyone with Manage Server (Administrators included) or
    the bot owner — every server can use completely different levels/roles."""
    settings = guild_settings.setdefault(str(ctx.guild.id), {})
    level_roles = settings.setdefault("level_roles", {})
    level_roles[str(level)] = role.id
    save_json(GUILD_SETTINGS_FILE, guild_settings)

    updated = await resync_all_level_roles(ctx.guild)

    embed = discord.Embed(
        description=f"✅ Reaching **Level {level}** or higher now grants {role.mention} in this server — like Arcane's level-role ladder, members only keep their highest-earned tier.",
        color=discord.Color.green(),
    )
    if updated:
        embed.set_footer(text=f"Applied retroactively — {updated} member(s) who already qualified were updated just now.")
    await ctx.send(embed=embed)


@bot.hybrid_command()
@commands.guild_only()
@has_permissions_or_owner(manage_guild=True)
async def removelevelrole(ctx, level: int):
    """Removes a level-up role reward from THIS server, and re-syncs anyone currently holding
    that tier down to whatever tier they now actually qualify for. Usage: !removelevelrole 15"""
    level_roles = guild_settings.get(str(ctx.guild.id), {}).get("level_roles", {})
    if str(level) not in level_roles:
        await ctx.send(embed=discord.Embed(description=f"No role is set for level {level} here.", color=discord.Color.red()))
        return
    del level_roles[str(level)]
    save_json(GUILD_SETTINGS_FILE, guild_settings)

    updated = await resync_all_level_roles(ctx.guild)

    embed = discord.Embed(description=f"🗑️ Removed the level {level} role reward.", color=discord.Color.orange())
    if updated:
        embed.set_footer(text=f"Re-synced {updated} member(s) who were holding that tier.")
    await ctx.send(embed=embed)


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


# ============================================================
# SERVER STATS CHANNELS — creates locked voice channels (like the "ServerStats" bot) whose
# NAMES show live counts. Nobody can actually join them; they're just for display. Names
# only refresh every ~10 minutes (Discord rate-limits channel renames hard — roughly 2 per
# 10 minutes per channel — so anything faster would just get throttled/dropped anyway).
# ============================================================
@bot.hybrid_command()
@commands.guild_only()
@has_permissions_or_owner(manage_guild=True)
async def setupserverstats(ctx):
    """Creates a locked category with 3 voice channels showing live member/human/bot counts
    (names only, like the ServerStats bot) — refreshes automatically every ~10 minutes.
    Usage: !setupserverstats"""
    guild = ctx.guild
    overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=True, connect=False)}
    try:
        category = await guild.create_category("📊 Server Stats", overwrites=overwrites, reason="Server stats setup")
        members_channel = await guild.create_voice_channel("👥 Members: ...", category=category, reason="Server stats setup")
        humans_channel = await guild.create_voice_channel("🧍 Humans: ...", category=category, reason="Server stats setup")
        bots_channel = await guild.create_voice_channel("🤖 Bots: ...", category=category, reason="Server stats setup")
    except discord.Forbidden:
        await ctx.send(embed=discord.Embed(title="⚠️ Error — NO_PERMISSION", description="I need Manage Channels permission to create these.", color=discord.Color.red()))
        return

    settings = guild_settings.setdefault(str(guild.id), {})
    settings["stats_channels"] = {
        "category": category.id, "members": members_channel.id,
        "humans": humans_channel.id, "bots": bots_channel.id,
    }
    save_json(GUILD_SETTINGS_FILE, guild_settings)
    await update_server_stats_for_guild(guild)
    await ctx.send(embed=discord.Embed(description="✅ Server stats channels created — they'll refresh automatically roughly every 10 minutes.", color=discord.Color.green()))


@bot.hybrid_command()
@commands.guild_only()
@has_permissions_or_owner(manage_guild=True)
async def removeserverstats(ctx):
    """Deletes the server stats category/channels created with !setupserverstats."""
    settings = guild_settings.get(str(ctx.guild.id), {})
    stats = settings.pop("stats_channels", None)
    save_json(GUILD_SETTINGS_FILE, guild_settings)
    if not stats:
        await ctx.send(embed=discord.Embed(description="No server stats channels are set up here.", color=discord.Color.greyple()))
        return
    for key in ("members", "humans", "bots", "category"):
        channel = ctx.guild.get_channel(stats.get(key))
        if channel:
            try:
                await channel.delete(reason="Server stats removed")
            except discord.HTTPException:
                pass
    await ctx.send(embed=discord.Embed(description="🗑️ Removed the server stats channels.", color=discord.Color.orange()))


async def update_server_stats_for_guild(guild: discord.Guild):
    stats = guild_settings.get(str(guild.id), {}).get("stats_channels")
    if not stats:
        return
    total = guild.member_count
    bots = sum(1 for m in guild.members if m.bot)
    humans = total - bots

    for key, label, count in (("members", "👥 Members", total), ("humans", "🧍 Humans", humans), ("bots", "🤖 Bots", bots)):
        channel = guild.get_channel(stats.get(key))
        if channel is None:
            continue
        new_name = f"{label}: {count}"
        if channel.name != new_name:  # only rename when the count actually changed
            try:
                await channel.edit(name=new_name)
            except discord.HTTPException:
                pass  # likely rate-limited — it'll catch up next cycle


# ============================================================
# ACTIVITY LEADERBOARDS & "MOST ACTIVE" ROLES — tracks raw chat/voice activity (separate
# from XP/levels, so it keeps working even with leveling turned off). The active-chat/
# active-voice/active-overall roles are GROUP roles: everyone who clears the activity bar
# holds it at once, not just a single #1. The Yapper roles are the opposite — single-holder,
# purely competitive "who talked the most today/this week", no minimum bar required.
# ============================================================
@bot.hybrid_command()
@commands.guild_only()
@has_permissions_or_owner(manage_guild=True)
@discord.app_commands.describe(role="Role to give EVERY member who's genuinely active in chat this week (leave blank to turn off)")
async def setactivechatrole(ctx, role: discord.Role = None):
    """Auto-gives a role ('Top Active Weekly' is a good name for it) to EVERY member who
    clears the chat-activity bar in the recent window (weekly by default) — if 8 people
    qualify, all 8 get it, not just #1. Taken back the moment someone stops qualifying.
    Usage: !setactivechatrole @Top Active Weekly"""
    settings = guild_settings.setdefault(str(ctx.guild.id), {})
    if role is None:
        settings.pop("active_chat_role", None)
        save_json(GUILD_SETTINGS_FILE, guild_settings)
        await ctx.send(embed=discord.Embed(description="🛑 Turned off the active-chatters role.", color=discord.Color.orange()))
        return
    settings["active_chat_role"] = role.id
    save_json(GUILD_SETTINGS_FILE, guild_settings)
    await sync_active_roles(ctx.guild)
    await ctx.send(embed=discord.Embed(description=f"✅ {role.mention} will now be given to everyone who's genuinely active in chat this week — updates roughly every 10 minutes.", color=discord.Color.green()))


@bot.hybrid_command()
@commands.guild_only()
@has_permissions_or_owner(manage_guild=True)
@discord.app_commands.describe(role="Role to give EVERY member who's genuinely active in voice this week (leave blank to turn off)")
async def setactivevoicerole(ctx, role: discord.Role = None):
    """Auto-gives a role ('Top Voice Active Weekly' is a good name for it) to EVERY member
    who clears the voice-activity bar in the recent window (weekly by default) — if 8 people
    qualify, all 8 get it, not just #1. Taken back the moment someone stops qualifying.
    Usage: !setactivevoicerole @Top Voice Active Weekly"""
    settings = guild_settings.setdefault(str(ctx.guild.id), {})
    if role is None:
        settings.pop("active_voice_role", None)
        save_json(GUILD_SETTINGS_FILE, guild_settings)
        await ctx.send(embed=discord.Embed(description="🛑 Turned off the active-in-voice role.", color=discord.Color.orange()))
        return
    settings["active_voice_role"] = role.id
    save_json(GUILD_SETTINGS_FILE, guild_settings)
    await sync_active_roles(ctx.guild)
    await ctx.send(embed=discord.Embed(description=f"✅ {role.mention} will now be given to everyone who's genuinely active in voice this week — updates roughly every 10 minutes.", color=discord.Color.green()))


@bot.hybrid_command()
@commands.guild_only()
@has_permissions_or_owner(manage_guild=True)
@discord.app_commands.describe(role="Role to give EVERY member who's genuinely active in BOTH chat and voice this week (leave blank to turn off)")
async def setactiveoverallrole(ctx, role: discord.Role = None):
    """Auto-gives a role to EVERY member who's genuinely active in BOTH chat AND voice this
    week (has to clear the chat bar AND the voice bar — not just one or the other) —
    everyone who qualifies gets it, not just #1. A different, separate role from
    !setactivechatrole / !setactivevoicerole, meant specifically to reward well-rounded
    activity across both. Usage: !setactiveoverallrole @Active Overall"""
    settings = guild_settings.setdefault(str(ctx.guild.id), {})
    if role is None:
        settings.pop("active_overall_role", None)
        save_json(GUILD_SETTINGS_FILE, guild_settings)
        await ctx.send(embed=discord.Embed(description="🛑 Turned off the active-overall role.", color=discord.Color.orange()))
        return
    settings["active_overall_role"] = role.id
    save_json(GUILD_SETTINGS_FILE, guild_settings)
    await sync_active_roles(ctx.guild)
    await ctx.send(embed=discord.Embed(description=f"✅ {role.mention} will now be given to everyone who's genuinely active overall this week — updates roughly every 10 minutes.", color=discord.Color.green()))


@bot.hybrid_command()
@commands.guild_only()
@has_permissions_or_owner(manage_guild=True)
@discord.app_commands.describe(role="Role for whoever's sent the most messages TODAY (leave blank to turn off)")
async def setyapperrole(ctx, role: discord.Role = None):
    """Auto-gives a role ('Certified Yapper' is a good name for it) to whoever's sent the
    most messages TODAY in this server — resets fresh every day, no minimum required, just
    whoever talked the most. Only one person holds it at a time.
    Usage: !setyapperrole @Certified Yapper"""
    settings = guild_settings.setdefault(str(ctx.guild.id), {})
    if role is None:
        settings.pop("yapper_role", None)
        save_json(GUILD_SETTINGS_FILE, guild_settings)
        await ctx.send(embed=discord.Embed(description="🛑 Turned off the daily top-chatter role.", color=discord.Color.orange()))
        return
    settings["yapper_role"] = role.id
    save_json(GUILD_SETTINGS_FILE, guild_settings)
    await sync_active_roles(ctx.guild)
    await ctx.send(embed=discord.Embed(description=f"✅ {role.mention} now goes to whoever's sent the most messages TODAY — resets daily.", color=discord.Color.green()))


@bot.hybrid_command()
@commands.guild_only()
@has_permissions_or_owner(manage_guild=True)
@discord.app_commands.describe(role="Role for whoever's sent the most messages THIS WEEK (leave blank to turn off)")
async def setgrandyapperrole(ctx, role: discord.Role = None):
    """Auto-gives a role ('Grand Yapper Supreme' is a good name for it) to whoever's sent
    the most messages THIS WEEK (a fixed 7-day window) in this server — no minimum
    required, just whoever talked the most. Only one person holds it at a time.
    Usage: !setgrandyapperrole @Grand Yapper Supreme"""
    settings = guild_settings.setdefault(str(ctx.guild.id), {})
    if role is None:
        settings.pop("grand_yapper_role", None)
        save_json(GUILD_SETTINGS_FILE, guild_settings)
        await ctx.send(embed=discord.Embed(description="🛑 Turned off the weekly top-chatter role.", color=discord.Color.orange()))
        return
    settings["grand_yapper_role"] = role.id
    save_json(GUILD_SETTINGS_FILE, guild_settings)
    await sync_active_roles(ctx.guild)
    await ctx.send(embed=discord.Embed(description=f"✅ {role.mention} now goes to whoever's sent the most messages THIS WEEK — resets weekly.", color=discord.Color.green()))


async def sync_active_roles(guild: discord.Guild):
    """Keeps the 'Top Active Weekly' / 'Top Voice Active Weekly' / overall-active roles held
    by EVERY member who currently clears the activity bar — not just #1. 8 people qualify?
    All 8 get it. Ranked over the last N days (default weekly), so it reflects who's
    genuinely active lately, not an all-time total someone could win once and keep forever.
    Also re-syncs the single-holder 'Certified Yapper' (today) and 'Grand Yapper Supreme'
    (this week) novelty roles, which work differently — see sync_yapper_roles below."""
    settings = guild_settings.get(str(guild.id), {})
    window_days = settings.get("active_window_days", ACTIVE_WINDOW_DEFAULT_DAYS)
    recent = get_recent_activity(guild.id, window_days)

    await apply_qualifying_group(guild, settings.get("active_chat_role"), {uid: d.get("messages", 0) for uid, d in recent.items()}, ACTIVE_ROLE_MIN_MESSAGES)
    await apply_qualifying_group(guild, settings.get("active_voice_role"), {uid: d.get("voice_seconds", 0) for uid, d in recent.items()}, ACTIVE_ROLE_MIN_VOICE_MINUTES * 60)

    # "Overall" means genuinely active in BOTH chat AND voice — a hard AND, not a blended
    # score. A blended score let someone qualify through chat activity alone (at a bar even
    # lower than the dedicated chat role's own bar), which meant a brand-new member who'd
    # never touched voice could still pick up "Active Overall". Requiring both individually
    # closes that off.
    overall_qualifiers = {
        uid: 1
        for uid, d in recent.items()
        if d.get("messages", 0) >= ACTIVE_ROLE_MIN_MESSAGES and d.get("voice_seconds", 0) >= ACTIVE_ROLE_MIN_VOICE_MINUTES * 60
    }
    await apply_qualifying_group(guild, settings.get("active_overall_role"), overall_qualifiers, 1)
    await sync_yapper_roles(guild)


async def apply_qualifying_group(guild: discord.Guild, role_id, scores: dict, minimum: float):
    """Gives the role to EVERY member whose score clears the minimum, and takes it back from
    anyone holding it who no longer does — a group of qualifiers, not a single #1."""
    if not role_id:
        return
    role = guild.get_role(role_id)
    if role is None:
        return

    qualifying_members = set()
    for user_id_str, score in scores.items():
        if score >= minimum:
            member = guild.get_member(int(user_id_str))
            if member:
                qualifying_members.add(member)

    for member in role.members:
        if member not in qualifying_members:
            try:
                await member.remove_roles(role, reason="No longer clears the recent activity bar")
            except discord.Forbidden:
                pass
    for member in qualifying_members:
        if role not in member.roles:
            try:
                await member.add_roles(role, reason="Clears the recent activity bar")
            except discord.Forbidden:
                pass


async def apply_single_top_holder(guild: discord.Guild, role_id, scores: dict, reason: str):
    """Gives the role to ONLY whoever scores highest — for the competitive/novelty Yapper
    roles, where it's specifically about being #1, not about clearing a bar."""
    if not role_id:
        return
    role = guild.get_role(role_id)
    if role is None:
        return

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    top_user_id = int(ranked[0][0]) if ranked and ranked[0][1] > 0 else None
    top_member = guild.get_member(top_user_id) if top_user_id else None

    for member in role.members:
        if member != top_member:
            try:
                await member.remove_roles(role, reason="No longer the top yapper")
            except discord.Forbidden:
                pass
    if top_member and role not in top_member.roles:
        try:
            await top_member.add_roles(role, reason=reason)
        except discord.Forbidden:
            pass


async def sync_yapper_roles(guild: discord.Guild):
    """'Certified Yapper' — whoever's sent the most messages TODAY (resets fresh every day).
    'Grand Yapper Supreme' — whoever's sent the most messages THIS WEEK (a fixed 7-day
    window, separate from the adjustable !setactivitywindow setting, since 'weekly' is part
    of the name). No minimum bar — purely 'who talked the most', just for fun."""
    settings = guild_settings.get(str(guild.id), {})

    today_scores = get_messages_for_day(guild.id, _today_key())
    await apply_single_top_holder(guild, settings.get("yapper_role"), today_scores, "Certified Yapper of the day")

    week_recent = get_recent_activity(guild.id, 7)
    week_scores = {uid: d.get("messages", 0) for uid, d in week_recent.items()}
    await apply_single_top_holder(guild, settings.get("grand_yapper_role"), week_scores, "Grand Yapper Supreme of the week")


@bot.hybrid_command()
@commands.guild_only()
@has_permissions_or_owner(manage_guild=True)
@discord.app_commands.describe(days="How many days of activity count as 'recent' for the active roles and leaderboards")
async def setactivitywindow(ctx, days: int):
    """Sets how many days of RECENT activity count toward the 'Top Active Weekly' / 'Top
    Voice Active Weekly' / overall-active GROUP roles in THIS server (default 7 — weekly).
    Doesn't affect !activeleaderboard, which always shows today/this-week/all-time
    regardless of this setting. Usage: !setactivitywindow 7"""
    if days < 1 or days > ACTIVITY_RETENTION_DAYS:
        await ctx.send(embed=discord.Embed(description=f"Pick something between 1 and {ACTIVITY_RETENTION_DAYS} days.", color=discord.Color.red()))
        return
    guild_settings.setdefault(str(ctx.guild.id), {})["active_window_days"] = days
    save_json(GUILD_SETTINGS_FILE, guild_settings)
    await sync_active_roles(ctx.guild)
    await ctx.send(embed=discord.Embed(description=f"✅ The active-chatters/active-voice/active-overall roles now look at the last **{days} day(s)**.", color=discord.Color.green()))


ACTIVITY_LEADERBOARD_SCOPES = [
    ("chat_today", "💬", "Chat — Today"),
    ("chat_weekly", "💬", "Chat — Weekly"),
    ("chat_alltime", "💬", "Chat — All Time"),
    ("vc_today", "🎙️", "Voice — Today"),
    ("vc_weekly", "🎙️", "Voice — Weekly"),
    ("vc_alltime", "🎙️", "Voice — All Time"),
]
ACTIVITY_SCOPE_LABELS = {key: f"{emoji} {label}" for key, emoji, label in ACTIVITY_LEADERBOARD_SCOPES}


def get_activity_scores_for_scope(guild_id, scope: str) -> dict:
    """Returns {user_id_str: raw_count} for one leaderboard scope — messages for chat
    scopes, voice_seconds for voice scopes."""
    if scope == "chat_today":
        return get_messages_for_day(guild_id, _today_key())
    if scope == "vc_today":
        return get_voice_seconds_for_day(guild_id, _today_key())
    if scope == "chat_weekly":
        recent = get_recent_activity(guild_id, 7)
        return {uid: d.get("messages", 0) for uid, d in recent.items() if d.get("messages", 0) > 0}
    if scope == "vc_weekly":
        recent = get_recent_activity(guild_id, 7)
        return {uid: d.get("voice_seconds", 0) for uid, d in recent.items() if d.get("voice_seconds", 0) > 0}
    if scope == "chat_alltime":
        return {uid: d.get("messages", 0) for uid, d in get_lifetime_activity(guild_id).items() if d.get("messages", 0) > 0}
    if scope == "vc_alltime":
        return {uid: d.get("voice_seconds", 0) for uid, d in get_lifetime_activity(guild_id).items() if d.get("voice_seconds", 0) > 0}
    return {}


def format_activity_value(scope: str, value) -> str:
    if scope.startswith("vc_"):
        hours, remainder = divmod(int(value), 3600)
        minutes = remainder // 60
        return f"{hours}h {minutes}m" if hours else f"{minutes}m"
    return f"{int(value)} msg"


def build_activity_leaderboard_embed(guild: discord.Guild, scope: str) -> discord.Embed:
    """Same visual style as the levels leaderboard — author line with the server icon, one
    field per rank, footer with the scope. Discord embeds can only show ONE big image, so
    everyone's avatar can't appear inline next to their own rank — the #1 member's avatar
    is shown as the thumbnail instead, as the closest equivalent."""
    scores = get_activity_scores_for_scope(guild.id, scope)
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:10]

    embed = discord.Embed(color=discord.Color.dark_teal())
    embed.set_author(name=f"{guild.name}'s activity leaderboard", icon_url=(guild.icon.url if guild.icon else bot.user.display_avatar.url))
    embed.description = f"**{ACTIVITY_SCOPE_LABELS[scope]}**"

    if not ranked:
        embed.add_field(name="\u200b", value="No activity tracked for this yet.", inline=False)
    else:
        for i, (user_id_str, value) in enumerate(ranked, start=1):
            embed.add_field(name="\u200b", value=f"**#{i}** • <@{user_id_str}> • {format_activity_value(scope, value)}", inline=False)
        top_member = guild.get_member(int(ranked[0][0]))
        if top_member:
            embed.set_thumbnail(url=top_member.display_avatar.url)

    embed.set_footer(text=f"Top {len(ranked)} member(s) — this server only")
    return embed


class ActivityLeaderboardSelect(discord.ui.Select):
    def __init__(self, guild: discord.Guild, current_scope: str):
        options = [
            discord.SelectOption(label=label, value=key, emoji=emoji, default=(key == current_scope))
            for key, emoji, label in ACTIVITY_LEADERBOARD_SCOPES
        ]
        super().__init__(placeholder=ACTIVITY_SCOPE_LABELS[current_scope], options=options, min_values=1, max_values=1)
        self.guild = guild

    async def callback(self, interaction: discord.Interaction):
        scope = self.values[0]
        await interaction.response.edit_message(embed=build_activity_leaderboard_embed(self.guild, scope), view=ActivityLeaderboardView(self.guild, scope))


class ActivityLeaderboardView(discord.ui.View):
    def __init__(self, guild: discord.Guild, current_scope: str):
        super().__init__(timeout=None)  # a finite timeout here caused "this interaction failed" — after the local timeout, discord.py stops tracking the view, so clicking the dropdown later gets no response at all and Discord shows a generic failure
        self.add_item(ActivityLeaderboardSelect(guild, current_scope))


@bot.hybrid_command()
@commands.guild_only()
@discord.app_commands.describe(scope="Which activity leaderboard to show (you can also switch with the dropdown after)")
async def activeleaderboard(ctx, scope: typing.Literal["chat_today", "chat_weekly", "chat_alltime", "vc_today", "vc_weekly", "vc_alltime"] = "chat_weekly"):
    """Shows THIS server's activity leaderboard — chat or voice, today/this week/all time.
    Usage: !activeleaderboard chat_weekly (or switch scopes with the dropdown afterward)."""
    await ctx.send(embed=build_activity_leaderboard_embed(ctx.guild, scope), view=ActivityLeaderboardView(ctx.guild, scope))


@tasks.loop(minutes=10)
async def server_stats_and_active_roles_update():
    """Every 10 minutes: refreshes any server-stats channel names, re-checks the
    most-active-chat/voice roles in every server that has them configured, and prunes old
    activity data."""
    prune_old_activity()
    for guild in bot.guilds:
        try:
            await update_server_stats_for_guild(guild)
            await sync_active_roles(guild)
        except Exception as e:
            print(f"⚠️ server_stats_and_active_roles_update failed for {guild.name}: {e}")


@bot.hybrid_command()
@commands.guild_only()
@has_permissions_or_owner(manage_guild=True)
async def setlevel(ctx, member: discord.Member, level: int):
    """Directly sets a member's level in THIS server (resets their XP progress to 0 for that
    level). Usage: !setlevel @someone 10"""
    if level < 0:
        await ctx.send(embed=discord.Embed(description="Level can't be negative.", color=discord.Color.red()))
        return
    if level > MAX_LEVEL:
        await ctx.send(embed=discord.Embed(description=f"That's way higher than needed — the cap is **{MAX_LEVEL}**.", color=discord.Color.red()))
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
    if user.id in users_being_globally_banned:
        return  # part of an in-progress !globalban/anti-raid global ban — that command sends one consolidated summary instead
    moderator, reason = await get_audit_actor(guild, discord.AuditLogAction.ban, user.id)
    await mod_log(guild, "Member Banned", user, moderator or "Unknown", reason or "No reason given", discord.Color.red())


@bot.event
async def on_member_unban(guild, user):
    moderator, reason = await get_audit_actor(guild, discord.AuditLogAction.unban, user.id)
    await mod_log(guild, "Member Unbanned", user, moderator or "Unknown", reason or "No reason given", discord.Color.green())

    if user.id in users_being_globally_unbanned:
        return  # this unban IS the intentional !globalunban — don't re-ban them right back
    if str(user.id) in global_bans_data:
        # Someone unbanned this account locally, but it's still on the global ban list —
        # the global ban should keep holding until it's actually lifted with !globalunban.
        ban_reason = global_bans_data[str(user.id)].get("reason", "No reason given")
        try:
            await guild.ban(user, reason=f"GLOBAL BAN (re-enforced — was manually unbanned locally): {ban_reason}")
            await dm_owner(f"🌐 **{user}** was manually unbanned in **{guild.name}**, but they're still on the global ban list — re-banned automatically. Use !globalunban if you want them actually unbanned everywhere.")
        except discord.Forbidden:
            await dm_owner(f"⚠️ **{user}** was manually unbanned in **{guild.name}** and is still globally banned, but I couldn't re-ban them there — missing permission.")


@bot.event
async def on_guild_channel_create(channel):
    """Makes sure new channels automatically get hidden from timed-out members too,
    so the Timed Out role doesn't need manual re-setup every time a channel is added."""
    timeout_role = get_timeout_role(channel.guild)
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

    # Enforce an active timeout against tampering — covers two different ways someone could
    # defeat it: (1) manually removing the Timed Out role itself, and (2) manually GIVING the
    # timed-out member some other role, which would leave them with real access despite still
    # technically being "timed out". Only applies while stored_roles still has them as active
    # — restore_roles deletes that entry FIRST when a timeout properly ends, specifically so
    # this block can't fight with a legitimate !untimeout or expiry.
    record = stored_roles.get(str(after.id))
    if not record or before.roles == after.roles:
        return

    timeout_role = get_timeout_role(after.guild)
    if timeout_role is None:
        return

    extra_roles = [r for r in after.roles if r != after.guild.default_role and r != timeout_role]
    missing_timeout_role = timeout_role not in after.roles

    if not extra_roles and not missing_timeout_role:
        return  # nothing to enforce — e.g. this update was our own restore_roles call

    try:
        if extra_roles:
            await after.remove_roles(*extra_roles, reason="Timeout still active — stripping role(s) granted while timed out")
        if missing_timeout_role:
            await after.add_roles(timeout_role, reason="Timeout role removed manually — restored, timeout is still active")
        if (extra_roles or missing_timeout_role) and after.voice and after.voice.channel:
            await after.move_to(None, reason="Timed out (tampered with while active)")
    except discord.Forbidden:
        pass


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
                    # Anti-raid bans are GLOBAL — a raid account gets banned everywhere the
                    # bot has a presence, not just the server that got raided.
                    global_bans_data[str(member.id)] = {"reason": reason, "banned_by": bot.user.id, "timestamp": datetime.datetime.now(datetime.timezone.utc).timestamp()}
                    save_json(GLOBAL_BANS_FILE, global_bans_data)
                    success, failed = await apply_global_ban(member.id, f"GLOBAL BAN (anti-raid auto-ban): {reason}")
                    await mod_log(member.guild, "Anti-Raid Auto-Ban (GLOBAL)", member, bot.user, f"{reason} — banned in {success}/{len(bot.guilds)} server(s)", discord.Color.dark_red())
                else:
                    await member.kick(reason=reason)
                    await mod_log(member.guild, "Anti-Raid Auto-Kick", member, bot.user, reason, discord.Color.dark_red())
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


@bot.hybrid_command()
@commands.guild_only()
@has_permissions_or_owner(manage_channels=True)
@discord.app_commands.describe(channel="The channel to lock (leave blank to lock the one you're in)")
async def lockchannel(ctx, channel: discord.TextChannel = None):
    """Makes ONE channel read-only for everyone (@everyone loses Send Messages there) —
    unlike !lockdown, this doesn't touch any other channel. Usage: !lockchannel #general"""
    channel = channel or ctx.channel
    overwrite = channel.overwrites_for(ctx.guild.default_role)
    overwrite.send_messages = False
    try:
        await channel.set_permissions(ctx.guild.default_role, overwrite=overwrite, reason=f"Locked by {ctx.author}")
    except discord.Forbidden:
        await ctx.send(embed=discord.Embed(title="⚠️ Error — NO_PERMISSION", description="I don't have permission to edit that channel's permissions.", color=discord.Color.red()))
        return
    await ctx.send(embed=discord.Embed(description=f"🔒 {channel.mention} is now locked — read-only for everyone.", color=discord.Color.dark_red()))
    await mod_log(ctx.guild, "Channel Locked", channel, ctx.author, "Manually locked", discord.Color.dark_red())


@bot.hybrid_command()
@commands.guild_only()
@has_permissions_or_owner(manage_channels=True)
@discord.app_commands.describe(channel="The channel to unlock (leave blank to unlock the one you're in)")
async def unlockchannel(ctx, channel: discord.TextChannel = None):
    """Restores normal send-message permissions to ONE channel that was locked with
    !lockchannel. Usage: !unlockchannel #general"""
    channel = channel or ctx.channel
    overwrite = channel.overwrites_for(ctx.guild.default_role)
    overwrite.send_messages = None
    try:
        await channel.set_permissions(ctx.guild.default_role, overwrite=overwrite, reason=f"Unlocked by {ctx.author}")
    except discord.Forbidden:
        await ctx.send(embed=discord.Embed(title="⚠️ Error — NO_PERMISSION", description="I don't have permission to edit that channel's permissions.", color=discord.Color.red()))
        return
    await ctx.send(embed=discord.Embed(description=f"🔓 {channel.mention} is unlocked again.", color=discord.Color.green()))
    await mod_log(ctx.guild, "Channel Unlocked", channel, ctx.author, "Manually unlocked", discord.Color.green())


# ============================================================
# FUN & EXTRAS
# ============================================================
class RPSView(discord.ui.View):
    """Rock-paper-scissors — pick a button, bot picks at the same time."""
    def __init__(self, author_id: int, timeout: float = 30):
        super().__init__(timeout=timeout)
        self.author_id = author_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Start your own game with `!rps`!", ephemeral=True)
            return False
        return True

    async def resolve(self, interaction: discord.Interaction, user_choice: str):
        bot_choice = random.choice(["🪨 Rock", "📄 Paper", "✂️ Scissors"])
        beats = {"🪨 Rock": "✂️ Scissors", "📄 Paper": "🪨 Rock", "✂️ Scissors": "📄 Paper"}
        if user_choice == bot_choice:
            outcome = "It's a tie!"
        elif beats[user_choice] == bot_choice:
            outcome = "You win! 🎉"
        else:
            outcome = "I win! 😎"
        for child in self.children:
            child.disabled = True
        embed = discord.Embed(description=f"You picked **{user_choice}**\nI picked **{bot_choice}**\n\n**{outcome}**", color=discord.Color.blurple())
        await interaction.response.edit_message(embed=embed, view=self)
        self.stop()

    @discord.ui.button(label="Rock", emoji="🪨", style=discord.ButtonStyle.secondary)
    async def rock(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.resolve(interaction, "🪨 Rock")

    @discord.ui.button(label="Paper", emoji="📄", style=discord.ButtonStyle.secondary)
    async def paper(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.resolve(interaction, "📄 Paper")

    @discord.ui.button(label="Scissors", emoji="✂️", style=discord.ButtonStyle.secondary)
    async def scissors(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.resolve(interaction, "✂️ Scissors")


@bot.hybrid_command()
async def rps(ctx):
    """Play rock-paper-scissors against the bot — click a button to choose. Usage: !rps"""
    await ctx.send(embed=discord.Embed(title="🪨📄✂️ Rock, Paper, Scissors", description="Pick your move!", color=discord.Color.blurple()), view=RPSView(author_id=ctx.author.id))


EIGHT_BALL_ANSWERS = [
    "It is certain.", "Without a doubt.", "You may rely on it.", "Yes, definitely.",
    "It is decidedly so.", "As I see it, yes.", "Most likely.", "Outlook good.",
    "Signs point to yes.", "Yes.", "Reply hazy, try again.", "Ask again later.",
    "Better not tell you now.", "Cannot predict now.", "Concentrate and ask again.",
    "Don't count on it.", "My reply is no.", "My sources say no.",
    "Outlook not so good.", "Very doubtful.",
]


@bot.hybrid_command(name="8ball")
@discord.app_commands.describe(question="What do you want to ask?")
async def eight_ball(ctx, *, question: str):
    """Ask the magic 8-ball a question. Usage: !8ball Will I win the lottery?"""
    embed = discord.Embed(color=discord.Color.blurple())
    embed.add_field(name="🎱 Question", value=question, inline=False)
    embed.add_field(name="Answer", value=random.choice(EIGHT_BALL_ANSWERS), inline=False)
    await ctx.send(embed=embed)


@bot.hybrid_command()
async def coinflip(ctx):
    """Flips a coin. Usage: !coinflip"""
    await ctx.send(embed=discord.Embed(description=f"🪙 **{random.choice(['Heads', 'Tails'])}!**", color=discord.Color.gold()))


@bot.hybrid_command()
@discord.app_commands.describe(dice="Format: NdN, e.g. 2d6 for two six-sided dice (defaults to 1d6)")
async def roll(ctx, dice: str = "1d6"):
    """Rolls dice. Usage: !roll 2d6"""
    match = re.match(r"^(\d{1,2})d(\d{1,4})$", dice.strip().lower())
    if not match:
        await ctx.send(embed=discord.Embed(description="Format is `NdN` — e.g. `2d6` or `1d20`.", color=discord.Color.red()))
        return
    count, sides = int(match.group(1)), int(match.group(2))
    if count < 1 or sides < 2:
        await ctx.send(embed=discord.Embed(description="Need at least 1 die with at least 2 sides.", color=discord.Color.red()))
        return
    rolls = [random.randint(1, sides) for _ in range(count)]
    embed = discord.Embed(
        title=f"🎲 Rolling {dice}",
        description=f"Result: {', '.join(str(r) for r in rolls)}\n**Total: {sum(rolls)}**",
        color=discord.Color.blurple(),
    )
    await ctx.send(embed=embed)


JOKES = [
    "Why don't scientists trust atoms? Because they make up everything.",
    "I told my computer I needed a break, and it said no problem — it froze immediately.",
    "Why do programmers prefer dark mode? Because light attracts bugs.",
    "I would tell you a UDP joke, but you might not get it.",
    "Why did the developer go broke? Because they used up all their cache.",
    "There are 10 types of people: those who understand binary, and those who don't.",
    "Why do Java developers wear glasses? Because they don't C#.",
    "A SQL query walks into a bar, walks up to two tables and asks: 'Can I join you?'",
]


@bot.hybrid_command()
async def joke(ctx):
    """Tells a random joke. Usage: !joke"""
    await ctx.send(embed=discord.Embed(description=random.choice(JOKES), color=discord.Color.blurple()))


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
    """Converts a level+xp pair into one cumulative XP number (used for the global
    leaderboard). Closed-form sum instead of looping `level` times — get_level_xp is just a
    quadratic, so the sum has an exact formula. The loop version was a real production bug:
    it ran synchronously with no `await` in it, so an unusually large level value turned
    this into a loop that ran for the better part of an hour, freezing the bot's entire
    event loop and blocking Discord gateway heartbeats (and every command, in every server)
    the whole time. This version is O(1) regardless of how large level gets."""
    n = level
    if n <= 0:
        return max(xp, 0)
    sum_of_squares = (n - 1) * n * (2 * n - 1) // 6
    sum_of_linear = (n - 1) * n // 2
    total = 5 * sum_of_squares + 50 * sum_of_linear + 100 * n
    return total + xp


def level_from_total(total_xp):
    """Reverses total_xp_for — turns a cumulative XP number back into a level + remaining xp.
    Binary search against the now-O(1) total_xp_for, instead of counting up one level at a
    time — same reasoning as total_xp_for above: an unbounded loop here is just as capable
    of freezing the bot if a combined total ever gets large enough."""
    if total_xp <= 0:
        return 0, max(total_xp, 0)
    lo, hi = 0, 1
    while total_xp_for(hi, 0) <= total_xp:
        hi *= 2
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if total_xp_for(mid, 0) <= total_xp:
            lo = mid
        else:
            hi = mid - 1
    return lo, total_xp - total_xp_for(lo, 0)


async def sync_level_roles_for_member(guild: discord.Guild, member: discord.Member, level: int):
    """Enforces the level-role LADDER for one member: gives them the role for the HIGHEST
    configured threshold they've reached or passed, and takes back any of this server's OTHER
    configured level-role rewards they're currently holding — so a member only ever holds
    their current tier, never every tier they've passed through. A level below every
    configured threshold means no level role at all (any they're holding get removed).
    Returns (added_role_or_None, [removed_role, ...])."""
    level_roles = guild_settings.get(str(guild.id), {}).get("level_roles", {})
    if not level_roles:
        return None, []

    qualifying = [(int(lvl_str), role_id) for lvl_str, role_id in level_roles.items() if int(lvl_str) <= level]
    target_role_id = max(qualifying, key=lambda pair: pair[0])[1] if qualifying else None
    target_role = guild.get_role(target_role_id) if target_role_id else None

    all_configured_role_ids = set(level_roles.values())
    to_remove = [r for r in member.roles if r.id in all_configured_role_ids and r != target_role]
    removed = []
    if to_remove:
        try:
            await member.remove_roles(*to_remove, reason="Level-role ladder: replaced by a higher tier")
            removed = to_remove
        except discord.Forbidden:
            pass  # couldn't take the old one(s) back — not fatal, just leaves an extra role

    added_role = None
    if target_role and target_role not in member.roles:
        try:
            await member.add_roles(target_role, reason=f"Reached level {level}")
            added_role = target_role
        except discord.Forbidden:
            await dm_owner(f"⚠️ Tried to give {member} the '{target_role.name}' role in {guild.name} but don't have permission.")

    return added_role, removed


async def resync_all_level_roles(guild: discord.Guild) -> int:
    """Re-applies the level-role ladder to every member who has XP data in this server —
    used right after !setlevelrole / !removelevelrole so the change takes effect immediately
    for people who already qualify, instead of waiting for their next level-up. Returns how
    many members were actually changed."""
    updated = 0
    for user_id_str, data in levels_data.get(str(guild.id), {}).items():
        member = guild.get_member(int(user_id_str))
        if member is None:
            continue
        added, removed = await sync_level_roles_for_member(guild, member, data["level"])
        if added or removed:
            updated += 1
    return updated


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

    starting_level = user_data["level"]
    # Recompute via total XP instead of looping level-by-level — same reasoning as the
    # total_xp_for/level_from_total fix above: !addxp with a huge number used to turn this
    # into a loop that ran once per level gained, which is exactly the kind of unbounded
    # synchronous loop that froze the bot for the better part of an hour last time. This is
    # O(log n) regardless of how big amount is.
    current_total = total_xp_for(user_data["level"], user_data["xp"])
    new_total = max(0, current_total + amount)
    user_data["level"], user_data["xp"] = level_from_total(new_total)
    if user_data["level"] > MAX_LEVEL:
        user_data["level"], user_data["xp"] = MAX_LEVEL, 0

    channel = get_guild_channel(guild.id, "level_up_channel") or announce_channel

    added_role, removed_roles = None, []
    if user_data["level"] > starting_level:
        # Threshold-based, ladder-style: this picks up the highest tier reached even if
        # several were passed at once, and takes back whichever lower tier they had before.
        added_role, removed_roles = await sync_level_roles_for_member(guild, member, user_data["level"])

    if user_data["level"] > starting_level and channel:
        if user_data["level"] == starting_level + 1:
            description = f"🎉 {member.mention} leveled up to **Level {user_data['level']}**!"
        else:
            description = f"🎉 {member.mention} leveled up from **Level {starting_level}** to **Level {user_data['level']}**!"
        embed = discord.Embed(description=description, color=discord.Color.green())
        if added_role:
            embed.add_field(name="🏅 New role earned", value=added_role.mention, inline=False)
        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            pass

    save_json(LEVELS_FILE, levels_data)


ACTIVE_WINDOW_DEFAULT_DAYS = 7   # "weekly" by default — how many days count as "recent" for the active roles/leaderboards
ACTIVE_ROLE_MIN_MESSAGES = 15    # need at least this many messages in the window to hold the chat-active role
ACTIVE_ROLE_MIN_VOICE_MINUTES = 20  # need at least this many voice minutes in the window to hold the voice-active role
ACTIVITY_RETENTION_DAYS = 60     # daily buckets older than this get pruned regardless of window, to keep the file small


def _today_key() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def _ensure_lifetime(user_activity: dict) -> dict:
    """Backfills the 'lifetime' totals field for activity records created before it existed,
    by summing whatever daily buckets are already there. Returns the lifetime dict — this
    is what makes 'all time' actually mean all time, surviving daily-bucket pruning."""
    if "lifetime" not in user_activity:
        daily = user_activity.get("daily", {})
        user_activity["lifetime"] = {
            "messages": sum(d.get("messages", 0) for d in daily.values()),
            "voice_seconds": sum(d.get("voice_seconds", 0) for d in daily.values()),
        }
    return user_activity["lifetime"]


def record_chat_activity(guild_id: int, user_id: int):
    guild_activity = activity_data.setdefault(str(guild_id), {})
    user_activity = guild_activity.setdefault(str(user_id), {"daily": {}})
    day = user_activity.setdefault("daily", {}).setdefault(_today_key(), {"messages": 0, "voice_seconds": 0})
    day["messages"] = day.get("messages", 0) + 1
    lifetime = _ensure_lifetime(user_activity)
    lifetime["messages"] = lifetime.get("messages", 0) + 1
    save_json(ACTIVITY_FILE, activity_data)


def record_voice_activity(guild_id: int, user_id: int, seconds: float):
    if seconds <= 0:
        return
    guild_activity = activity_data.setdefault(str(guild_id), {})
    user_activity = guild_activity.setdefault(str(user_id), {"daily": {}})
    day = user_activity.setdefault("daily", {}).setdefault(_today_key(), {"messages": 0, "voice_seconds": 0})
    day["voice_seconds"] = day.get("voice_seconds", 0) + seconds
    lifetime = _ensure_lifetime(user_activity)
    lifetime["voice_seconds"] = lifetime.get("voice_seconds", 0) + seconds
    save_json(ACTIVITY_FILE, activity_data)


def get_recent_activity(guild_id, window_days: int):
    """Returns {user_id_str: {'messages': int, 'voice_seconds': float}} summed over just the
    last window_days — this is what makes 'most active' mean CURRENTLY/genuinely active,
    rather than whoever racked up the biggest all-time total once and went quiet."""
    cutoff = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=window_days)).strftime("%Y-%m-%d")
    result = {}
    for user_id_str, user_activity in activity_data.get(str(guild_id), {}).items():
        messages = 0
        voice_seconds = 0
        for day_key, day_data in user_activity.get("daily", {}).items():
            if day_key >= cutoff:
                messages += day_data.get("messages", 0)
                voice_seconds += day_data.get("voice_seconds", 0)
        if messages or voice_seconds:
            result[user_id_str] = {"messages": messages, "voice_seconds": voice_seconds}
    return result


def get_lifetime_activity(guild_id) -> dict:
    """Returns {user_id_str: {'messages': int, 'voice_seconds': float}} — true all-time
    totals, unaffected by daily-bucket pruning."""
    result = {}
    for user_id_str, user_activity in activity_data.get(str(guild_id), {}).items():
        lifetime = _ensure_lifetime(user_activity)
        if lifetime.get("messages") or lifetime.get("voice_seconds"):
            result[user_id_str] = lifetime
    return result


def get_messages_for_day(guild_id, day_key: str) -> dict:
    """Returns {user_id_str: messages} for just ONE specific day — used by the daily
    'Certified Yapper' role, which resets fresh every day."""
    result = {}
    for user_id_str, user_activity in activity_data.get(str(guild_id), {}).items():
        day_data = user_activity.get("daily", {}).get(day_key)
        if day_data and day_data.get("messages", 0) > 0:
            result[user_id_str] = day_data["messages"]
    return result


def get_voice_seconds_for_day(guild_id, day_key: str) -> dict:
    """Returns {user_id_str: voice_seconds} for just ONE specific day."""
    result = {}
    for user_id_str, user_activity in activity_data.get(str(guild_id), {}).items():
        day_data = user_activity.get("daily", {}).get(day_key)
        if day_data and day_data.get("voice_seconds", 0) > 0:
            result[user_id_str] = day_data["voice_seconds"]
    return result


def prune_old_activity():
    """Drops daily activity buckets older than ACTIVITY_RETENTION_DAYS so the file doesn't
    grow forever — run periodically, not on every message."""
    cutoff = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=ACTIVITY_RETENTION_DAYS)).strftime("%Y-%m-%d")
    changed = False
    for guild_activity in activity_data.values():
        for user_activity in guild_activity.values():
            daily = user_activity.get("daily", {})
            for day_key in list(daily.keys()):
                if day_key < cutoff:
                    del daily[day_key]
                    changed = True
    if changed:
        save_json(ACTIVITY_FILE, activity_data)


async def add_xp(message):
    guild_id = str(message.guild.id)
    record_chat_activity(message.guild.id, message.author.id)
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
    if minutes_elapsed > 0:
        record_voice_activity(guild.id, member.id, minutes_elapsed * 60)
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
        super().__init__(timeout=None)  # same "this interaction failed" bug as ActivityLeaderboardView had — a finite timeout stops the dropdown from working after enough idle time
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
    top_member = ctx.guild.get_member(int(sorted_users[0][0]))
    if top_member:
        embed.set_thumbnail(url=top_member.display_avatar.url)

    embed.set_footer(text=f"Top {len(sorted_users)} members by level — this server only")
    await ctx.send(embed=embed, view=LeaderboardView())


@bot.hybrid_command()
@commands.guild_only()
@owner_only()
@discord.app_commands.describe(member="Whose invite count to check in this server (leave blank for your own)")
async def invites(ctx, member: discord.Member = None):
    """Bot-creator only. Shows how many current members someone has invited to THIS server.
    Usage: !invites @someone. This is a running total of successful joins credited to them —
    it doesn't subtract people who later left."""
    member = member or ctx.author
    count = invite_data.get(str(ctx.guild.id), {}).get("invite_counts", {}).get(str(member.id), 0)
    await ctx.send(embed=discord.Embed(description=f"📨 {member.mention} has invited **{count}** member(s) to **{ctx.guild.name}**.", color=discord.Color.blurple()))


@bot.hybrid_command()
@commands.guild_only()
@owner_only()
async def inviteleaderboard(ctx):
    """Bot-creator only. Shows THIS server's top inviters. Usage: !inviteleaderboard"""
    counts = invite_data.get(str(ctx.guild.id), {}).get("invite_counts", {})
    if not counts:
        await ctx.send(embed=discord.Embed(description="No invites tracked yet in this server.", color=discord.Color.greyple()))
        return
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:10]
    lines = []
    for i, (user_id_str, count) in enumerate(ranked, start=1):
        member = ctx.guild.get_member(int(user_id_str))
        label = member.mention if member else f"<@{user_id_str}> (left server)"
        lines.append(f"**{i}.** {label} — {count} invite(s)")
    embed = discord.Embed(title=f"📨 Top Inviters — {ctx.guild.name}", description="\n".join(lines), color=discord.Color.gold())
    embed.set_footer(text="Running totals of successful joins — not reduced when someone later leaves.")
    await ctx.send(embed=embed)


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


def parse_hex_color(text: str):
    """Parses a hex color string like '#5865F2' or '5865F2' into a discord.Color.
    Returns None if text is empty/invalid, so callers can fall back to a default color."""
    if not text:
        return None
    cleaned = text.strip().lstrip("#")
    if len(cleaned) != 6:
        return None
    try:
        return discord.Color(int(cleaned, 16))
    except ValueError:
        return None


@bot.hybrid_command()
@commands.guild_only()
@has_permissions_or_owner(manage_guild=True)
@discord.app_commands.describe(
    duration="How long the giveaway runs, e.g. 10m, 2h, 1d, 1d12h",
    winners="How many winners to pick",
    prize="What's being given away",
    image="Optional image URL — shown as a big banner on the giveaway embed",
    color="Optional hex color for the embed's side bar, e.g. #FF6EC7",
)
async def giveaway(ctx, duration: str, winners: int, prize: str, image: str = None, color: str = None):
    """Starts a giveaway with a real 'Enter' button (click again to leave). Usage:
    !giveaway 1h 1 Nitro Classic
    Duration examples: 30s, 10m, 2h, 1d, or combined like 1d12h."""
    seconds = parse_duration(duration)
    if not seconds:
        await ctx.send(embed=discord.Embed(description="Couldn't read that duration — try `10m`, `2h`, `1d`, or `1d12h`.", color=discord.Color.red()))
        return
    if winners < 1:
        await ctx.send(embed=discord.Embed(description="Needs at least 1 winner.", color=discord.Color.red()))
        return

    end_time = datetime.datetime.now(datetime.timezone.utc).timestamp() + seconds
    embed_color = parse_hex_color(color) or discord.Color.fuchsia()
    embed = discord.Embed(
        title="🎉 GIVEAWAY 🎉",
        description=(f"**{prize}**\n\nClick the button below to enter!\n"
                      f"Ends: <t:{int(end_time)}:R>\nWinners: **{winners}**\nHosted by: {ctx.author.mention}"),
        color=embed_color,
    )
    if image:
        embed.set_image(url=image)
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label="🎉 Enter", style=discord.ButtonStyle.success, custom_id="giveaway_enter:pending"))
    msg = await ctx.send(embed=embed, view=view)

    # The button's custom_id needs the real message ID, which only exists after sending —
    # so edit it in now that we have one.
    view.children[0].custom_id = f"giveaway_enter:{msg.id}"
    await msg.edit(view=view)

    giveaways_data[str(msg.id)] = {
        "guild_id": ctx.guild.id, "channel_id": ctx.channel.id, "prize": prize,
        "winners": winners, "end_time": end_time, "host_id": ctx.author.id, "entrants": [],
    }
    save_json(GIVEAWAYS_FILE, giveaways_data)


def compute_giveaway_weight(guild: discord.Guild, user_id_str: str, entry_record) -> float:
    """Returns 0 if the member left the server or holds a blacklisted role; otherwise their
    entry count plus any bonus entries from their roles or a personal bonus, using THIS
    server's giveaway settings."""
    member = guild.get_member(int(user_id_str))
    if member is None:
        return 0
    settings = guild_settings.get(str(guild.id), {})
    blacklist = set(settings.get("giveaway_blacklist_roles", []))
    if any(r.id in blacklist for r in member.roles):
        return 0
    base = entry_record.get("count", 1) if isinstance(entry_record, dict) else 1
    bonus_roles = settings.get("giveaway_bonus_roles", {})
    role_bonus = sum(bonus_roles.get(str(r.id), 0) for r in member.roles)
    member_bonus = settings.get("giveaway_bonus_members", {}).get(user_id_str, 0)
    return max(0, base + role_bonus + member_bonus)


def weighted_sample_without_replacement(weighted_items: dict, k: int):
    """weighted_items: {item: weight}. Returns up to k unique items sampled without
    replacement — higher weight means more likely to be picked, but nobody can win twice."""
    pool = list(weighted_items.items())
    winners = []
    for _ in range(min(k, len(pool))):
        total = sum(w for _, w in pool)
        if total <= 0:
            break
        r = random.uniform(0, total)
        upto = 0
        for i, (item, w) in enumerate(pool):
            upto += w
            if upto >= r:
                winners.append(item)
                pool.pop(i)
                break
    return winners


def normalize_giveaway_entrants(data: dict) -> dict:
    """Returns data's entrants as the current {user_id_str: {"count", "last_entry_day"}}
    shape, converting on the fly if it's still the old plain-list format."""
    entrants = data.get("entrants", {})
    if isinstance(entrants, list):
        entrants = {str(uid): {"count": 1} for uid in entrants}
    return entrants


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

    entrants = normalize_giveaway_entrants(data)
    weights = {uid: compute_giveaway_weight(guild, uid, record) for uid, record in entrants.items()}
    weights = {uid: w for uid, w in weights.items() if w > 0}

    if weights:
        pick_count = min(data["winners"], len(weights))
        winner_ids = weighted_sample_without_replacement(weights, pick_count)
        winner_mentions = ", ".join(f"<@{uid}>" for uid in winner_ids)
        result_text = f"🎉 Congrats {winner_mentions}! You won **{data['prize']}**!"
    else:
        winner_mentions = "None — no valid entries"
        result_text = f"No valid entries — nobody won **{data['prize']}**."

    ended_embed = discord.Embed(
        title="🎉 GIVEAWAY ENDED 🎉",
        description=f"**{data['prize']}**\n\nWinner(s): {winner_mentions}\nTotal entrants: {len(entrants)}",
        color=discord.Color.dark_grey(),
    )
    try:
        await message.edit(embed=ended_embed, view=None)
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
    """Ends a giveaway early. Usage: !gend <message_id or link>"""
    parsed_id = parse_message_id(message_id)
    if parsed_id is None:
        await ctx.send(embed=discord.Embed(description="Couldn't read a message ID out of that — paste the raw ID or the giveaway message's link.", color=discord.Color.red()))
        return
    data = giveaways_data.get(str(parsed_id))
    if not data:
        await ctx.send(embed=discord.Embed(description="No active giveaway with that message ID.", color=discord.Color.red()))
        return
    await end_giveaway(str(parsed_id), data)
    await ctx.send(embed=discord.Embed(description="✅ Giveaway ended.", color=discord.Color.green()))


@bot.hybrid_command()
@has_permissions_or_owner(manage_guild=True)
async def greroll(ctx, message_id: str):
    """Re-picks a winner for an ALREADY-ENDED giveaway, using the same entrant/weighting data.
    Usage: !greroll <message_id or link>"""
    parsed_id = parse_message_id(message_id)
    if parsed_id is None:
        await ctx.send(embed=discord.Embed(description="Couldn't read a message ID out of that — paste the raw ID or the giveaway message's link.", color=discord.Color.red()))
        return
    data = giveaways_data.get(str(parsed_id))
    entrants = normalize_giveaway_entrants(data) if data else {}
    if not entrants:
        await ctx.send(embed=discord.Embed(description="No stored entrants to reroll from for that giveaway (either it's too old, or nobody entered).", color=discord.Color.red()))
        return
    weights = {uid: compute_giveaway_weight(ctx.guild, uid, record) for uid, record in entrants.items()}
    weights = {uid: w for uid, w in weights.items() if w > 0}
    if not weights:
        await ctx.send(embed=discord.Embed(description="Nobody who entered is still eligible (left the server, or now blacklisted).", color=discord.Color.red()))
        return
    winner_id = weighted_sample_without_replacement(weights, 1)[0]
    await ctx.send(embed=discord.Embed(description=f"🎉 New winner: <@{winner_id}>!", color=discord.Color.fuchsia()))


@bot.hybrid_command()
@commands.guild_only()
@has_permissions_or_owner(manage_guild=True)
async def giveawayentrants(ctx, message_id: str):
    """Shows who's entered an active giveaway, and how many EFFECTIVE entries each person
    has — base entry plus any bonus entries from !setgiveawaybonusrole/!setgiveawaybonusmember
    — since that's what's actually used when a winner gets picked. Usage: !giveawayentrants <message_id or link>"""
    parsed_id = parse_message_id(message_id)
    if parsed_id is None:
        await ctx.send(embed=discord.Embed(description="Couldn't read a message ID out of that — paste the raw ID or the giveaway message's link.", color=discord.Color.red()))
        return
    data = giveaways_data.get(str(parsed_id))
    if not data:
        await ctx.send(embed=discord.Embed(description="No active giveaway with that message ID.", color=discord.Color.red()))
        return
    entrants = normalize_giveaway_entrants(data)
    if not entrants:
        await ctx.send(embed=discord.Embed(description=f"Nobody has entered **{data['prize']}** yet.", color=discord.Color.greyple()))
        return

    weights = {uid: compute_giveaway_weight(ctx.guild, uid, record) for uid, record in entrants.items()}
    total_entries = sum(weights.values())
    lines = []
    for uid, weight in weights.items():
        weight = int(weight)
        note = "" if weight > 0 else " *(ineligible — left the server, or blacklisted)*"
        lines.append(f"<@{uid}> — {weight} entr{'y' if weight == 1 else 'ies'}{note}")
    shown = lines[:25]
    description = "\n".join(shown)
    if len(lines) > 25:
        description += f"\n…and {len(lines) - 25} more"
    embed = discord.Embed(title=f"🎉 Entrants — {data['prize']}", description=description, color=discord.Color.blurple())
    embed.set_footer(text=f"{len(entrants)} unique member(s), {int(total_entries)} total effective entries")
    await ctx.send(embed=embed)


@bot.hybrid_command()
@commands.guild_only()
@has_permissions_or_owner(manage_guild=True)
@discord.app_commands.describe(role="Role to toggle on/off the giveaway blacklist")
async def togglegiveawayblacklist(ctx, role: discord.Role):
    """Blocks (or unblocks) a role from entering any giveaway in THIS server at all.
    Usage: !togglegiveawayblacklist @Muted"""
    settings = guild_settings.setdefault(str(ctx.guild.id), {})
    blacklist = settings.setdefault("giveaway_blacklist_roles", [])
    if role.id in blacklist:
        blacklist.remove(role.id)
        save_json(GUILD_SETTINGS_FILE, guild_settings)
        await ctx.send(embed=discord.Embed(description=f"✅ {role.mention} can enter giveaways again.", color=discord.Color.green()))
    else:
        blacklist.append(role.id)
        save_json(GUILD_SETTINGS_FILE, guild_settings)
        await ctx.send(embed=discord.Embed(description=f"🚫 {role.mention} is now blocked from entering giveaways.", color=discord.Color.orange()))


@bot.hybrid_command()
@commands.guild_only()
@has_permissions_or_owner(manage_guild=True)
@discord.app_commands.describe(role="Role to grant bonus giveaway entries to", extra_entries="How many EXTRA entries on top of the normal 1 (0 removes the bonus)")
async def setgiveawaybonusrole(ctx, role: discord.Role, extra_entries: int):
    """Gives everyone with this role extra giveaway entries in THIS server.
    Usage: !setgiveawaybonusrole @VIP 2"""
    settings = guild_settings.setdefault(str(ctx.guild.id), {})
    bonus = settings.setdefault("giveaway_bonus_roles", {})
    if extra_entries <= 0:
        bonus.pop(str(role.id), None)
        save_json(GUILD_SETTINGS_FILE, guild_settings)
        await ctx.send(embed=discord.Embed(description=f"🛑 Removed the giveaway bonus from {role.mention}.", color=discord.Color.orange()))
        return
    bonus[str(role.id)] = extra_entries
    save_json(GUILD_SETTINGS_FILE, guild_settings)
    await ctx.send(embed=discord.Embed(description=f"✅ {role.mention} now gets **+{extra_entries}** bonus giveaway entries.", color=discord.Color.green()))


@bot.hybrid_command()
@commands.guild_only()
@has_permissions_or_owner(manage_guild=True)
@discord.app_commands.describe(member="Member to grant bonus giveaway entries to", extra_entries="How many EXTRA entries on top of the normal 1 (0 removes the bonus)")
async def setgiveawaybonusmember(ctx, member: discord.Member, extra_entries: int):
    """Gives one specific member extra giveaway entries in THIS server.
    Usage: !setgiveawaybonusmember @someone 3"""
    settings = guild_settings.setdefault(str(ctx.guild.id), {})
    bonus = settings.setdefault("giveaway_bonus_members", {})
    if extra_entries <= 0:
        bonus.pop(str(member.id), None)
        save_json(GUILD_SETTINGS_FILE, guild_settings)
        await ctx.send(embed=discord.Embed(description=f"🛑 Removed the giveaway bonus from {member.mention}.", color=discord.Color.orange()))
        return
    bonus[str(member.id)] = extra_entries
    save_json(GUILD_SETTINGS_FILE, guild_settings)
    await ctx.send(embed=discord.Embed(description=f"✅ {member.mention} now gets **+{extra_entries}** bonus giveaway entries.", color=discord.Color.green()))


@bot.hybrid_command()
@commands.guild_only()
@has_permissions_or_owner(manage_guild=True)
@discord.app_commands.describe(state="Turn daily bonus entries on or off")
async def togglegiveawaydailyentries(ctx, state: typing.Literal["on", "off"]):
    """When ON, clicking Enter on a DIFFERENT day than your last click adds another entry
    (stacking daily) instead of just toggling you in/out once. Clicking again on the SAME
    day still leaves the giveaway. Usage: !togglegiveawaydailyentries on"""
    guild_settings.setdefault(str(ctx.guild.id), {})["giveaway_daily_entries"] = (state == "on")
    save_json(GUILD_SETTINGS_FILE, guild_settings)
    await ctx.send(embed=discord.Embed(description=f"✅ Daily bonus entries are now **{state}** for giveaways in this server.", color=discord.Color.green()))


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
def get_timeout_role(guild: discord.Guild):
    """Returns this server's configured timeout role if one's been set with !settimeoutrole,
    otherwise falls back to a role literally named 'Timed Out' if one already exists.
    Returns None if neither exists — use get_or_create_timeout_role when one must exist."""
    role_id = guild_settings.get(str(guild.id), {}).get("timeout_role_id")
    role = guild.get_role(role_id) if role_id else None
    if role:
        return role
    return discord.utils.get(guild.roles, name=TIMEOUT_ROLE_NAME)


async def setup_timeout_role_permissions(guild: discord.Guild, role: discord.Role):
    """(Re)applies the timeout permission setup to a role across every channel — hidden
    everywhere except the configured timeout channel, where it's visible but silent. Safe
    to call any time, not just once — e.g. !settimeoutchannel calls this again so changing
    the timeout channel later actually updates which channel is exempted."""
    timeout_channel = get_guild_channel(guild.id, "timeout_channel")
    for channel in guild.channels:
        try:
            if timeout_channel and channel.id == timeout_channel.id:
                await channel.set_permissions(role, view_channel=True, send_messages=False, speak=False, add_reactions=False)
            else:
                await channel.set_permissions(role, view_channel=False, send_messages=False, speak=False)
        except discord.Forbidden:
            pass


async def get_or_create_timeout_role(guild: discord.Guild):
    """Returns this server's timeout role, auto-creating one named 'Timed Out' (and setting
    up its permissions across every channel) the first time it's ever needed, if nothing's
    configured with !settimeoutrole and no role named 'Timed Out' already exists."""
    role = get_timeout_role(guild)
    if role is None:
        role = await guild.create_role(name=TIMEOUT_ROLE_NAME, reason="Auto-created for timeout system")
        await setup_timeout_role_permissions(guild, role)
    return role


async def custom_timeout(member: discord.Member, guild: discord.Guild, minutes: int, reason: str = "No reason given", moderator=None):
    timeout_role = await get_or_create_timeout_role(guild)
    timeout_channel = get_guild_channel(guild.id, "timeout_channel")

    current_role_ids = [role.id for role in member.roles if role != guild.default_role]
    stored_roles[str(member.id)] = {
        "role_ids": current_role_ids,
        "guild_id": guild.id,
        "issued_by": moderator.id if moderator else bot.user.id,
        "expires_at": datetime.datetime.now(datetime.timezone.utc).timestamp() + minutes * 60,
    }
    save_json(ROLES_FILE, stored_roles)

    roles_to_remove = [r for r in member.roles if r != guild.default_role]
    await member.remove_roles(*roles_to_remove, reason=reason)
    await member.add_roles(timeout_role, reason=reason)

    # A text-channel timeout doesn't stop someone from talking in voice — disconnect them too.
    if member.voice and member.voice.channel:
        try:
            await member.move_to(None, reason="Timed out")
        except discord.Forbidden:
            pass

    await mod_log(guild, "Member Timed Out", member, moderator or bot.user, f"{reason} ({minutes} min)", discord.Color.orange())

    if timeout_channel:
        embed = discord.Embed(
            title="⏱️ Member Timed Out",
            description=f"{member.mention} has been timed out for **{minutes} minute(s)**.\nReason: {reason}",
            color=discord.Color.orange(),
        )
        await timeout_channel.send(embed=embed)

    # No sleep here — timeout_expiry_check (a periodic task, like the giveaway system
    # already does) restores them once expires_at passes. A sleep tied to this one command
    # invocation would be silently lost forever if the bot restarted mid-timeout, leaving
    # someone stuck in the Timed Out role permanently until someone noticed and manually
    # ran !untimeout. Persisting the expiry means a restart just picks up where it left off.


async def restore_roles(member: discord.Member, guild: discord.Guild):
    # Delete the "still actively timed out" record FIRST, before touching any roles — this
    # is what stops the timeout-enforcement logic in on_member_update from fighting with us:
    # if it ran to check between our two role changes below and the record still existed,
    # it would see the member's roles temporarily changing and think someone was tampering,
    # putting the timeout role right back on even though we're the ones ending it properly.
    record = stored_roles.pop(str(member.id), None)
    if record is not None:
        save_json(ROLES_FILE, stored_roles)

    timeout_role = get_timeout_role(guild)
    if timeout_role:
        # Unconditional — don't gate this on "if timeout_role in member.roles" first. That
        # check can read a stale cached member (e.g. right after someone else's role edit
        # hasn't fully propagated yet), silently skipping the removal and leaving the
        # timeout role stuck on even though the timeout is supposed to be over. Removing a
        # role a member doesn't actually have is a harmless no-op, so there's no downside.
        try:
            await member.remove_roles(timeout_role, reason="Timeout expired")
        except discord.Forbidden:
            pass

    if record:
        saved_ids = record.get("role_ids", []) if isinstance(record, dict) else record  # tolerate the old plain-list format
        roles = [guild.get_role(rid) for rid in saved_ids if guild.get_role(rid)]
        if roles:
            try:
                await member.add_roles(*roles, reason="Timeout expired — restoring roles")
            except discord.Forbidden:
                pass

    await mod_log(guild, "Timeout Expired — Roles Restored", member, bot.user, "Automatic", discord.Color.green())


@tasks.loop(seconds=30)
async def timeout_expiry_check():
    """Restores anyone whose timeout has expired. Persisted via expires_at (checked here)
    instead of an in-memory sleep tied to the original command call — so a bot restart
    mid-timeout doesn't leave someone stuck in the Timed Out role forever."""
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    for user_id_str, record in list(stored_roles.items()):
        if not isinstance(record, dict) or record.get("expires_at") is None:
            continue  # tolerate old records from before this field existed — those need a manual !untimeout
        if record["expires_at"] > now:
            continue
        guild = bot.get_guild(record.get("guild_id"))
        if guild is None:
            del stored_roles[user_id_str]
            save_json(ROLES_FILE, stored_roles)
            continue
        member = guild.get_member(int(user_id_str))
        if member is None:
            del stored_roles[user_id_str]
            save_json(ROLES_FILE, stored_roles)
            continue
        await restore_roles(member, guild)


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
    """Lifts an active timeout early. If the bot owner personally issued it, only the bot
    owner can lift it early — a regular moderator can't override an owner-issued timeout."""
    record = stored_roles.get(str(member.id))
    if isinstance(record, dict) and record.get("issued_by") == OWNER_ID and ctx.author.id != OWNER_ID:
        await ctx.send(embed=discord.Embed(title="⚠️ Error — NOT_ALLOWED", description="This timeout was issued by the bot owner — only they can lift it early.", color=discord.Color.red()))
        return
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
# GLOBAL BANS — bot-creator only. Bans someone from EVERY server the bot is in at once, and
# remembers them so they're auto-banned in any server the bot joins later, or if they try to
# rejoin anywhere. This is what anti-raid auto-bans use too — a raid ban isn't just local to
# the server that got raided, it follows that account everywhere the bot has a presence.
# ============================================================
users_being_globally_banned = set()    # suppresses the per-guild "Member Banned" mod-log DM while a single !globalban (or anti-raid global ban) is banning across every server — one consolidated summary gets sent instead of one per server
users_being_globally_unbanned = set()  # suppresses the auto-re-ban-if-still-globally-banned enforcement while !globalunban is intentionally lifting a ban


async def apply_global_ban(user_id: int, reason: str):
    """Bans a user from every server the bot is currently in. Returns (success_count, [server
    names it couldn't ban in])."""
    success = 0
    failed = []
    user_obj = discord.Object(id=user_id)
    users_being_globally_banned.add(user_id)
    try:
        for guild in bot.guilds:
            try:
                await guild.ban(user_obj, reason=reason)
                success += 1
            except discord.HTTPException:
                failed.append(guild.name)
    finally:
        users_being_globally_banned.discard(user_id)
    return success, failed


@bot.hybrid_command()
@owner_only()
@discord.app_commands.describe(user="The user to ban from every server", reason="Why they're being globally banned")
async def globalban(ctx, user: discord.User, *, reason: str = "No reason given"):
    """Bot-creator only. Bans this user from EVERY server the bot is currently in, and
    remembers them so they're auto-banned in any server the bot joins later, or if they try
    to rejoin anywhere it's already in. Usage: !globalban @troublemaker Cross-server harassment"""
    global_bans_data[str(user.id)] = {"reason": reason, "banned_by": ctx.author.id, "timestamp": datetime.datetime.now(datetime.timezone.utc).timestamp()}
    save_json(GLOBAL_BANS_FILE, global_bans_data)

    success, failed = await apply_global_ban(user.id, f"GLOBAL BAN by bot owner: {reason}")

    embed = discord.Embed(
        title="🌐 Global Ban Applied",
        description=f"Banned **{user}** in **{success}/{len(bot.guilds)}** server(s).\nReason: {reason}",
        color=discord.Color.dark_red(),
    )
    if failed:
        embed.add_field(name="⚠️ Couldn't ban in", value=", ".join(failed)[:1024], inline=False)
    await ctx.send(embed=embed)


@bot.hybrid_command()
@owner_only()
@discord.app_commands.describe(user="The user to lift the global ban from")
async def globalunban(ctx, user: discord.User):
    """Bot-creator only. Removes a global ban — unbans them in every server where they were
    banned via !globalban (or an anti-raid global ban), and stops auto-banning them in the
    future. Usage: !globalunban @user"""
    was_tracked = global_bans_data.pop(str(user.id), None)
    save_json(GLOBAL_BANS_FILE, global_bans_data)

    success = 0
    failed = []
    users_being_globally_unbanned.add(user.id)
    try:
        for guild in bot.guilds:
            try:
                await guild.unban(user, reason=f"Global ban lifted by bot owner ({ctx.author})")
                success += 1
            except discord.NotFound:
                pass  # wasn't banned there — nothing to do
            except discord.HTTPException:
                failed.append(guild.name)
    finally:
        users_being_globally_unbanned.discard(user.id)

    embed = discord.Embed(description=f"✅ Lifted the global ban on **{user}** — unbanned in {success} server(s) where they were banned.", color=discord.Color.green())
    if not was_tracked:
        embed.description += "\n(They weren't on the tracked global-ban list, but I tried unbanning everywhere just in case.)"
    if failed:
        embed.add_field(name="⚠️ Couldn't unban in", value=", ".join(failed)[:1024], inline=False)
    await ctx.send(embed=embed)


@bot.hybrid_command()
@owner_only()
async def globalbanlist(ctx):
    """Bot-creator only. Lists everyone currently on the global ban list. Usage: !globalbanlist"""
    if not global_bans_data:
        await ctx.send(embed=discord.Embed(description="No one is globally banned right now.", color=discord.Color.greyple()))
        return
    lines = [f"<@{uid}> (`{uid}`) — {data.get('reason', 'No reason given')}" for uid, data in global_bans_data.items()]
    await ctx.send(embed=discord.Embed(title="🌐 Global Ban List", description="\n".join(lines)[:4000], color=discord.Color.dark_red()))


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


def find_backup_matches(backup_name: str, from_server_hint: str = None):
    """Searches EVERY server's backup folder for one matching this name (case-insensitively).
    Used only for the bot owner's cross-server restore — a regular server owner only ever
    looks inside their own server's folder. Returns a list of (guild_id_str, path) tuples;
    if from_server_hint is given, only folders matching that guild's ID or name (substring,
    case-insensitive) are considered."""
    normalized = normalize_backup_name(backup_name)
    matches = []
    if not os.path.isdir(BACKUPS_FOLDER):
        return matches
    for guild_id_str in os.listdir(BACKUPS_FOLDER):
        folder = os.path.join(BACKUPS_FOLDER, guild_id_str)
        if not os.path.isdir(folder):
            continue
        path = os.path.join(folder, f"{normalized}.json")
        if not os.path.exists(path):
            continue
        if from_server_hint:
            hint = from_server_hint.strip().lower()
            guild_obj = bot.get_guild(int(guild_id_str)) if guild_id_str.isdigit() else None
            id_match = guild_id_str == from_server_hint.strip()
            name_match = guild_obj is not None and hint in guild_obj.name.lower()
            if not (id_match or name_match):
                continue
        matches.append((guild_id_str, path))
    return matches


@bot.hybrid_command()
@commands.guild_only()
@backup_permission()
@discord.app_commands.describe(
    backup_name="Which saved backup to restore (leave blank to list your saves)",
    what="Restore everything, or only roles, or only channels/categories",
    from_server="Bot-creator only: pull this backup from a DIFFERENT server (name or ID) than the one you're running this in",
)
async def restoreserver(
    ctx,
    backup_name: str = None,
    what: typing.Literal["all", "roles", "channels"] = "all",
    from_server: str = None,
):
    """Recreates roles and/or channels (with permissions) from a saved backup INTO THIS server.
    Usage: !restoreserver mybackup [all|roles|channels]
    - all (default): roles + categories + channels + member role assignments
    - roles: only recreates roles and re-assigns them to current members — no channels/categories
    - channels: only recreates categories + channels (with permission overwrites) — no roles,
      no member role re-assignment. If a channel's saved overwrite references a role that isn't
      already in this server, that specific overwrite is just skipped.
    Run with no name to see your current saves. This server's own owner can only restore THIS
    server's own saves, into THIS server. You (the bot creator) can restore ANY backup you've
    ever made — from any server — into whatever server you run this in (handy for cloning a
    setup into a brand-new server); if the same name exists in more than one server, add
    `from_server` to say which one you mean.
    Only rebuilds structure + role assignments — does not restore messages."""
    is_owner = ctx.author.id == OWNER_ID
    all_backups = list_backup_names(ctx.guild.id)

    if backup_name is None:
        embed = discord.Embed(title="📁 Your Saves", color=discord.Color.blurple())
        if not all_backups:
            embed.description = "You don't have any backups saved yet — use `!backupserver <name>` first."
        else:
            embed.description = "\n".join(f"- `{b}`" for b in all_backups) + "\n\nRun `!restoreserver <name>` to restore one (add `roles` or `channels` to only restore part of it)."
        if is_owner:
            embed.set_footer(text="You can also pull a backup from a different server — add from_server to specify which one, or !listbackups to see everything you've saved everywhere.")
        await ctx.send(embed=embed)
        return

    source_guild_id = ctx.guild.id
    path = os.path.join(guild_backup_folder(ctx.guild.id), f"{normalize_backup_name(backup_name)}.json")

    if not os.path.exists(path) and is_owner:
        # Not in this server's own folder — as the bot creator, search every server you've
        # ever backed up, so you can clone a saved setup into a brand-new/different server.
        matches = find_backup_matches(backup_name, from_server)
        if len(matches) == 1:
            guild_id_str, path = matches[0]
            source_guild_id = int(guild_id_str)
        elif len(matches) > 1:
            options = []
            for guild_id_str, _ in matches:
                g = bot.get_guild(int(guild_id_str))
                options.append(f"- `{g.name if g else 'Unknown server'}` (`{guild_id_str}`)")
            await ctx.send(embed=discord.Embed(
                title="⚠️ Found that name in multiple servers",
                description=f"`{backup_name}` exists in more than one place:\n" + "\n".join(options) + "\n\nRe-run with `from_server` set to the name or ID of the one you want.",
                color=discord.Color.orange(),
            ))
            return

    if not os.path.exists(path):
        hint = " (checked every server you've backed up too)" if is_owner else ""
        embed = discord.Embed(
            description=f"❌ No backup found named `{backup_name}`{hint}.\n📁 Your saves here: {', '.join(f'`{b}`' for b in all_backups) if all_backups else '(none yet)'}",
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

    cross_server_note = ""
    if source_guild_id != ctx.guild.id:
        source_guild = bot.get_guild(source_guild_id)
        cross_server_note = f" (cloned from **{source_guild.name if source_guild else source_guild_id}**)"
    await ctx.send(embed=discord.Embed(description=f"🔧 Restoring `{backup_name}`{cross_server_note} ({what_label}) into **{guild.name}**... this may take a bit.", color=discord.Color.blurple()))

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
    await mod_log(guild, "Server Restored From Backup", guild.me, ctx.author, f"Backup: {backup_name} ({what_label}){cross_server_note}", discord.Color.blue())


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
    GUILD_SETTINGS_FILE, BIRTHDAYS_FILE, GIVEAWAYS_FILE, STARBOARD_FILE, INVITES_FILE, GLOBAL_BANS_FILE, ACTIVITY_FILE, ROLE_MENUS_FILE,
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
    refresh(invite_data, INVITES_FILE)
    refresh(global_bans_data, GLOBAL_BANS_FILE)
    refresh(activity_data, ACTIVITY_FILE)
    refresh(role_menus_data, ROLE_MENUS_FILE)


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
        channel = await get_announcement_channel(guild)
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


@bot.hybrid_command(name="arrowz")
@commands.guild_only()
@commands.cooldown(1, 60, commands.BucketType.user)
@discord.app_commands.allowed_installs(guilds=True, users=False)
@discord.app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
@discord.app_commands.describe(message="What you want to say to Arrowz (the bot's creator)")
async def arrowz(ctx, *, message: str):
    """Sends a message directly to Arrowz (the bot's creator) — they'll see who sent it and
    from which server/channel. Only works inside an actual server the bot is in — someone
    who just has this bot installed as a personal app (not added to any server) can't use
    this to message Arrowz out of nowhere. Usage: !arrowz Hey, love the bot! Could you add X?"""
    embed = discord.Embed(title="📩 New message via !arrowz", description=message, color=discord.Color.blurple())
    embed.add_field(name="From", value=f"{ctx.author} (`{ctx.author.id}`)", inline=False)
    embed.add_field(name="Server", value=f"{ctx.guild.name} (`{ctx.guild.id}`)", inline=True)
    embed.add_field(name="Channel", value=f"#{ctx.channel.name}", inline=True)
    embed.set_footer(text="Sent via !arrowz")
    try:
        owner = await bot.fetch_user(OWNER_ID)
        await owner.send(embed=embed)
    except discord.Forbidden:
        await ctx.send(embed=discord.Embed(title="⚠️ Error — DM_CLOSED", description="Couldn't deliver your message right now — Arrowz's DMs might be closed. Try again later.", color=discord.Color.red()))
        return
    await ctx.send(embed=discord.Embed(description="✅ Sent! Arrowz will see your message.", color=discord.Color.green()))


@bot.hybrid_command()
@commands.guild_only()
@owner_only()
@discord.app_commands.describe(channel="Voice channel to join (leave blank to join whichever VC you're currently in)")
async def joinvc(ctx, channel: discord.VoiceChannel = None):
    """Bot-creator only. Joins a voice channel in THIS server and STAYS connected — if it
    ever gets disconnected (network hiccup, a host restart, Discord booting bots left alone
    in a channel, etc), it automatically rejoins within ~2 minutes, no need to re-run this.
    Usage: !joinvc #general-voice, or just !joinvc while you're sitting in a voice channel
    yourself. Needs PyNaCl installed (add it to requirements.txt) — voice doesn't work
    without it.
    Heads up: this only helps if whatever tracks your 'streak' counts ANY member's presence
    in the channel — trackers that specifically check for YOUR account won't be fooled by
    the bot sitting in there instead of you."""
    if channel is None:
        author_voice_state = ctx.author.voice
        if author_voice_state is None or author_voice_state.channel is None:
            await ctx.send(embed=discord.Embed(description="Join a voice channel first, or tell me which one: `!joinvc #channel`.", color=discord.Color.red()))
            return
        channel = author_voice_state.channel

    existing = ctx.guild.voice_client
    try:
        if existing and existing.is_connected():
            await existing.move_to(channel)
        else:
            await channel.connect(reconnect=True)
    except (discord.ClientException, RuntimeError) as e:
        missing_pkg = "PyNaCl" if "PyNaCl" in str(e) else ("davey" if "davey" in str(e) else None)
        if missing_pkg:
            await ctx.send(embed=discord.Embed(title="⚠️ Error — MISSING_DEPENDENCY", description=f"This host doesn't have **{missing_pkg}** installed, which voice requires. Add `{missing_pkg}` to requirements.txt and do a full reinstall (not just a restart) — voice can't work without it.", color=discord.Color.red()))
        else:
            await ctx.send(embed=discord.Embed(title="⚠️ Error — VOICE_ERROR", description=f"Couldn't join — {e}", color=discord.Color.red()))
        return
    except asyncio.TimeoutError:
        await ctx.send(embed=discord.Embed(title="⚠️ Error — VOICE_TIMEOUT", description="Timed out trying to connect to that voice channel.", color=discord.Color.red()))
        return

    guild_settings.setdefault(str(ctx.guild.id), {})["persistent_vc_channel_id"] = channel.id
    save_json(GUILD_SETTINGS_FILE, guild_settings)
    await ctx.send(embed=discord.Embed(description=f"🔊 Joined {channel.mention} and staying connected — will auto-rejoin if disconnected for any reason, until you run !leavevc.", color=discord.Color.green()))


@bot.hybrid_command()
@commands.guild_only()
@owner_only()
async def leavevc(ctx):
    """Bot-creator only. Disconnects the bot from voice in THIS server, and stops the
    auto-rejoin behavior from !joinvc. Usage: !leavevc"""
    guild_settings.get(str(ctx.guild.id), {}).pop("persistent_vc_channel_id", None)
    save_json(GUILD_SETTINGS_FILE, guild_settings)

    voice_client = ctx.guild.voice_client
    if voice_client is None or not voice_client.is_connected():
        await ctx.send(embed=discord.Embed(description="I'm not in a voice channel here.", color=discord.Color.greyple()))
        return
    channel_name = voice_client.channel.name
    await voice_client.disconnect(force=True)
    await ctx.send(embed=discord.Embed(description=f"👋 Left `#{channel_name}`.", color=discord.Color.orange()))


@tasks.loop(minutes=2)
async def voice_reconnect_check():
    """If !joinvc was used in a server, keeps the bot connected to that channel —
    automatically reconnects if it ever gets disconnected (network drop, host restart,
    Discord disconnecting a bot left alone in a channel, etc), instead of silently staying
    disconnected until someone notices."""
    for guild in bot.guilds:
        channel_id = guild_settings.get(str(guild.id), {}).get("persistent_vc_channel_id")
        if not channel_id:
            continue
        channel = guild.get_channel(channel_id)
        if channel is None:
            continue  # the channel itself got deleted — nothing to rejoin until !joinvc is re-run elsewhere

        voice_client = guild.voice_client
        if voice_client and voice_client.is_connected() and voice_client.channel and voice_client.channel.id == channel_id:
            continue  # already exactly where it should be

        try:
            if voice_client and voice_client.is_connected():
                await voice_client.move_to(channel)
            else:
                await channel.connect(reconnect=True)
        except Exception as e:
            print(f"⚠️ voice_reconnect_check failed for {guild.name}: {e}")


# ============================================================
# "ANNI" — bot-creator-only, permanent deletion commands. Named after "annihilate": these
# don't archive or soft-delete anything, they call Discord's real delete endpoints. There is
# no undo beyond restoring a backup made BEFORE running one of these (!backupserver /
# !restoreserver). !anniserver asks for button confirmation first since it wipes everything.
# ============================================================
class ConfirmView(discord.ui.View):
    """A simple Confirm/Cancel button pair, restricted to whoever triggered it. Session-only
    (not persistent across restarts) since it's meant for an immediate one-shot decision,
    not something that should still be clickable days later."""
    def __init__(self, author_id: int, timeout: float = 30):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.confirmed = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This confirmation isn't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = True
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = False
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()


@bot.hybrid_command()
@commands.guild_only()
@owner_only()
@discord.app_commands.describe(role="The role to permanently delete")
async def annirole(ctx, role: discord.Role):
    """Bot-creator only. Permanently deletes a single role — gone, not archived.
    Usage: !annirole @SomeRole"""
    if role.is_default():
        await ctx.send(embed=discord.Embed(description="Can't delete `@everyone` — it isn't a real role, just the server's base permission set.", color=discord.Color.red()))
        return

    name = role.name
    guilds_in_bulk_delete.add(ctx.guild.id)  # suppress the normal single-role mod-log DM, we send our own summary below
    try:
        await role.delete(reason=f"Annihilated by bot owner ({ctx.author})")
    except discord.Forbidden:
        await ctx.send(embed=discord.Embed(title="⚠️ Error — NO_PERMISSION", description="I don't have permission to delete that role — it may be managed by an integration, or above my own role in the list.", color=discord.Color.red()))
        return
    finally:
        guilds_in_bulk_delete.discard(ctx.guild.id)  # always clear the flag, even on an unexpected error — otherwise this guild's mod-log DMs stay silently suppressed forever
    await ctx.send(embed=discord.Embed(description=f"💥 Deleted role `@{name}`.", color=discord.Color.dark_red()))


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
        await ctx.send(embed=discord.Embed(title="⚠️ Error — NO_PERMISSION", description="I don't have permission to delete that channel.", color=discord.Color.red()))
        return
    finally:
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
    try:
        for ch in channels:
            try:
                await ch.delete(reason=f"Annihilated by bot owner ({ctx.author}) — category wipe")
                deleted += 1
            except discord.HTTPException:
                pass
        try:
            await category.delete(reason=f"Annihilated by bot owner ({ctx.author})")
        except discord.Forbidden:
            await ctx.send(embed=discord.Embed(title="⚠️ Error — NO_PERMISSION", description=f"Deleted {deleted}/{len(channels)} channel(s) inside, but couldn't delete the category itself.", color=discord.Color.red()))
            return
    finally:
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
    THIS server — gone, not archived. Asks you to click Confirm first since it cannot be
    undone (short of restoring a backup made beforehand). Usage: !anniserver"""
    guild = ctx.guild
    view = ConfirmView(author_id=ctx.author.id, timeout=30)
    confirm_msg = await ctx.send(embed=discord.Embed(
        title="⚠️ Are you absolutely sure?",
        description=f"This will permanently delete **every channel, category, and custom role** in **{guild.name}**. This cannot be undone.\n\nClick **Confirm** within 30 seconds to proceed.",
        color=discord.Color.red(),
    ), view=view)

    timed_out = await view.wait()
    if timed_out:
        await confirm_msg.edit(embed=discord.Embed(description="⏱️ Timed out — nothing was deleted.", color=discord.Color.greyple()), view=None)
        return
    if not view.confirmed:
        await confirm_msg.edit(embed=discord.Embed(description="Cancelled — nothing was deleted.", color=discord.Color.greyple()), view=None)
        return

    guilds_in_bulk_delete.add(guild.id)
    deleted_channels = 0
    deleted_roles = 0
    try:
        for channel in list(guild.channels):
            try:
                await channel.delete(reason=f"Server annihilated by bot owner ({ctx.author})")
                deleted_channels += 1
            except discord.HTTPException:
                pass

        for role in list(guild.roles):
            if role.is_default() or role.managed:
                continue
            try:
                await role.delete(reason=f"Server annihilated by bot owner ({ctx.author})")
                deleted_roles += 1
            except discord.HTTPException:
                pass
    finally:
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
