# =============================================================================
#  ModMail Bot  —  bot.py
#  A Discord modmail bot that routes user tickets to private staff threads.
#
#  HOW IT WORKS:
#   1. Post the ticket panel in a channel with  /setup
#   2. Users click a button → fill a short form → ticket thread opens
#   3. The user chats with the bot via DM — messages relay to the thread
#   4. Staff reply in the thread — bot forwards replies back to the user's DM
#   5. Close the ticket with the Close button — thread locks, transcript saved
#
#  SETUP CHECKLIST:
#   1. Copy config.py.example → config.py and fill in your values
#   2. Create one text channel per ticket category in your Discord server
#   3. Set channel permissions so only the right role(s) can see each channel
#   4. Give the bot these permissions in each ticket channel:
#        View Channel, Send Messages, Create Public Threads,
#        Send Messages in Threads, Manage Threads
#   5. Run the bot and use  /setup  in your support channel
# =============================================================================

import discord
from discord.ext import commands, tasks
import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from io import BytesIO

# Load config from config.py
from config import (
    TOKEN, GUILD_ID, LOG_CHANNEL_ID,
    AUTO_CLOSE_HOURS, COOLDOWN_MINUTES,
    ADMIN_ROLE_ID,
    TICKET_CHANNELS,  # dict of category_key → channel_id
    TICKET_ROLES,     # dict of category_key → role_id
    SATELLITE_SERVERS,
)


# =============================================================================
#  TICKET CATEGORIES
#  Each entry defines one button on the panel.
#
#  Keys you can customise per category:
#    label      — button text shown in the server panel
#    emoji      — emoji prefix used in thread names
#    color      — embed color (hex int)
#    label_dm   — the name shown to the user in DM replies, e.g. "Support Team"
# =============================================================================
TICKET_CATEGORIES = {
    "general": {
        "label":    "💬 General Support",
        "emoji":    "💬",
        "color":    0x5865F2,   # blurple
        "label_dm": "Support Team",
    },
    "report": {
        "label":    "🚨 Report",
        "emoji":    "🚨",
        "color":    0xED4245,   # red
        "label_dm": "Report Team",
    },
    "suggestion": {
        "label":    "💡 Suggestion",
        "emoji":    "💡",
        "color":    0xFEE75C,   # yellow
        "label_dm": "Team",
    },
    "other": {
        "label":    "📩 Other",
        "emoji":    "📩",
        "color":    0x57F287,   # green
        "label_dm": "Team",
    },
}
# NOTE: You can add or remove categories here.
# If you change keys (e.g. "general"), also update TICKET_CHANNELS
# and TICKET_ROLES in config.py to match.

PRIORITY_LEVELS = {
    "low":    {"emoji": "🟢", "color": 0x57F287, "label": "Low"},
    "medium": {"emoji": "🟡", "color": 0xFEE75C, "label": "Medium"},
    "high":   {"emoji": "🟠", "color": 0xFFA500, "label": "High"},
    "urgent": {"emoji": "🔴", "color": 0xED4245, "label": "Urgent"},
}


# =============================================================================
#  BOT SETUP
# =============================================================================
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)


# =============================================================================
#  IN-MEMORY STATE
#  Everything the bot needs to track while running.
#  This resets on restart — persistent data (ticket count, bans) is in JSON files.
# =============================================================================

# Maps user_id → ticket info dict while a ticket is open
# { thread, category, priority, claimed_by, opened_at, origin_guild_id }
open_tickets: dict = {}

# Maps thread_id → user_id so we know whose ticket a thread belongs to
ticket_threads: dict = {}

# Maps thread_id → list of note dicts  [{mod, note, time}]
ticket_notes: dict = {}

# Maps thread_id → datetime of last message (used for auto-close)
last_activity: dict = {}

# Set of user IDs who are banned from opening tickets
banned_users: set = set()

# Maps user_id → datetime of last ticket open (for cooldown enforcement)
cooldowns: dict = {}

# DM flow tracking — when a user DMs the bot before a ticket is open
pending_choices: set = set()   # users who haven been shown the type menu
dm_initial_msg:  dict = {}     # user_id → their first message text
dm_initial_atts: dict = {}     # user_id → their first message attachments
dm_origin_guild: dict = {}     # user_id → guild_id they appeared to come from

# Persistent stats (saved to JSON)
ticket_stats: dict = {
    "total":   0,
    "closed":  0,
    "ratings": [],
    "by_type": {k: 0 for k in TICKET_CATEGORIES},
}

# File paths for persistent data
COUNTER_FILE = "ticket_counter.json"
BANNED_FILE  = "banned_users.json"


def load_persistent():
    """Load ticket stats and ban list from disk on startup."""
    global ticket_stats, banned_users
    if os.path.exists(COUNTER_FILE):
        with open(COUNTER_FILE) as f:
            data = json.load(f)
            ticket_stats.update(data)
            # Make sure every current category has a counter entry
            for k in TICKET_CATEGORIES:
                ticket_stats["by_type"].setdefault(k, 0)
    if os.path.exists(BANNED_FILE):
        with open(BANNED_FILE) as f:
            banned_users = set(json.load(f))


def save_persistent():
    """Write ticket stats and ban list to disk."""
    with open(COUNTER_FILE, "w") as f:
        json.dump(ticket_stats, f)
    with open(BANNED_FILE, "w") as f:
        json.dump(list(banned_users), f)


# =============================================================================
#  HELPER FUNCTIONS
# =============================================================================

def is_admin(member) -> bool:
    """Returns True if the member is the server owner or has Administrator."""
    if not member:
        return False
    if hasattr(member, "guild") and member.guild.owner_id == member.id:
        return True
    if hasattr(member, "guild_permissions") and member.guild_permissions.administrator:
        return True
    return False


def has_role(member, role_id: int) -> bool:
    """Returns True if the member has a specific role by ID."""
    return any(r.id == role_id for r in getattr(member, "roles", []))


def can_access_ticket(member, category: str) -> bool:
    """
    Returns True if the member can manage tickets of this category.
    Admins can always access everything.
    Otherwise checks the category's assigned role from config.
    """
    if is_admin(member):
        return True
    role_id = TICKET_ROLES.get(category)
    if role_id and has_role(member, role_id):
        return True
    # Admin role always gets access as fallback
    if has_role(member, ADMIN_ROLE_ID):
        return True
    return False


def can_access_any(member) -> bool:
    """Returns True if the member can manage at least one ticket category."""
    if is_admin(member):
        return True
    return any(can_access_ticket(member, cat) for cat in TICKET_CATEGORIES)


def get_thread_label(category: str) -> str:
    """Returns the DM-facing team label for a category, e.g. 'Support Team'."""
    return TICKET_CATEGORIES.get(category, {}).get("label_dm", "Staff")


def get_server_name(guild_id: int) -> str:
    """
    Returns a human-readable server name for a guild ID.
    Checks the bot's cache first (most reliable), then the config fallback dict.
    """
    guild = bot.get_guild(guild_id)
    if guild:
        return guild.name
    return SATELLITE_SERVERS.get(guild_id, f"Server {guild_id}")


def next_ticket_number() -> int:
    """Increments and returns the global ticket counter."""
    ticket_stats["total"] += 1
    save_persistent()
    return ticket_stats["total"]


def fmt_time(dt: datetime) -> str:
    """Formats a datetime object as a readable UTC string."""
    return dt.strftime("%Y-%m-%d %H:%M UTC") if dt else "N/A"


def check_cooldown(uid: int) -> int:
    """Returns seconds remaining on a user's cooldown, or 0 if they're clear."""
    if uid in cooldowns:
        elapsed   = (datetime.now(timezone.utc) - cooldowns[uid]).total_seconds()
        remaining = COOLDOWN_MINUTES * 60 - elapsed
        if remaining > 0:
            return int(remaining)
    return 0


# =============================================================================
#  DISCORD UI — VIEWS & MODALS
# =============================================================================

# -----------------------------------------------------------------------------
# 1. Ticket Panel View
#    This is the persistent button panel posted by /setup.
#    It lives forever (timeout=None) and survives bot restarts.
# -----------------------------------------------------------------------------
class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def _check_eligibility(self, interaction: discord.Interaction) -> bool:
        """
        Runs before opening a ticket modal.
        Returns False (and sends an error) if the user is banned, already has
        an open ticket, or is on cooldown.
        """
        uid = interaction.user.id

        if uid in banned_users:
            await interaction.response.send_message(embed=discord.Embed(
                title="🚫 Banned",
                description="You are not allowed to open tickets.",
                color=0xED4245), ephemeral=True)
            return False

        if uid in open_tickets:
            await interaction.response.send_message(embed=discord.Embed(
                title="⚠️ Ticket Already Open",
                description="You already have an open ticket.\nCheck your **DMs with this bot** to continue.",
                color=0xED4245), ephemeral=True)
            return False

        secs = check_cooldown(uid)
        if secs:
            await interaction.response.send_message(embed=discord.Embed(
                title="⏳ Cooldown",
                description=f"Please wait **{secs}s** before opening another ticket.",
                color=0xED4245), ephemeral=True)
            return False

        return True

    async def _open_modal(self, interaction: discord.Interaction, category: str):
        if not await self._check_eligibility(interaction):
            return
        await interaction.response.send_modal(OpenTicketModal(category))

    # One button per category — custom_id must be unique and stable across restarts
    @discord.ui.button(label="💬 General Support", style=discord.ButtonStyle.primary,   custom_id="panel:general",    row=0)
    async def btn_general(self, i, b):    await self._open_modal(i, "general")

    @discord.ui.button(label="🚨 Report",          style=discord.ButtonStyle.danger,    custom_id="panel:report",     row=0)
    async def btn_report(self, i, b):     await self._open_modal(i, "report")

    @discord.ui.button(label="💡 Suggestion",      style=discord.ButtonStyle.success,   custom_id="panel:suggestion", row=1)
    async def btn_suggestion(self, i, b): await self._open_modal(i, "suggestion")

    @discord.ui.button(label="📩 Other",            style=discord.ButtonStyle.secondary, custom_id="panel:other",      row=1)
    async def btn_other(self, i, b):      await self._open_modal(i, "other")


# -----------------------------------------------------------------------------
# 2. Open Ticket Modal
#    Pops up when a user clicks a panel button.
#    Asks for a short description of their issue.
# -----------------------------------------------------------------------------
class OpenTicketModal(discord.ui.Modal):
    message = discord.ui.TextInput(
        label="Describe your issue",
        style=discord.TextStyle.paragraph,
        placeholder="Give us a brief description so we can help you faster…",
        max_length=1000,
        required=True,
    )

    def __init__(self, category: str):
        cfg = TICKET_CATEGORIES[category]
        super().__init__(title=f"Open Ticket — {cfg['label']}")
        self.category = category

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        cooldowns[interaction.user.id] = datetime.now(timezone.utc)

        success, msg, thread = await create_ticket(
            user=interaction.user,
            category=self.category,
            initial_text=self.message.value,
            origin_guild_id=interaction.guild_id,
        )

        if success:
            await interaction.followup.send(embed=discord.Embed(
                title="✅ Ticket Opened!",
                description=(
                    "Your ticket has been created.\n\n"
                    "📬 **Check your DMs from this bot** — that's where you'll chat with the team."
                ),
                color=0x57F287), ephemeral=True)
        else:
            await interaction.followup.send(embed=discord.Embed(
                title="❌ Error", description=msg, color=0xED4245), ephemeral=True)


# -----------------------------------------------------------------------------
# 3. DM Category View
#    Shown when a user DMs the bot directly (no panel button clicked).
#    Lets them choose which team they want to reach.
# -----------------------------------------------------------------------------
class DMCategoryView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=600)  # 10 minute timeout
        self.user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Prevent other users from clicking someone else's menu
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ This menu isn't for you.", ephemeral=True)
            return False
        return True

    async def _create_from_dm(self, interaction: discord.Interaction, category: str):
        await interaction.response.defer()
        # Grab the stored initial message and attachments, then clear them
        text       = dm_initial_msg.pop(self.user_id, "")
        atts       = dm_initial_atts.pop(self.user_id, [])
        origin_gid = dm_origin_guild.pop(self.user_id, None)
        pending_choices.discard(self.user_id)

        success, msg, thread = await create_ticket(
            user=interaction.user,
            category=category,
            initial_text=text,
            initial_atts=atts,
            origin_guild_id=origin_gid,
        )

        cfg = TICKET_CATEGORIES[category]
        if success:
            await interaction.edit_original_response(embed=discord.Embed(
                title=f"✅ Ticket Opened — {cfg['label']}",
                description="The team will reply shortly.\nJust keep sending messages here.",
                color=cfg["color"]), view=None)
        else:
            await interaction.edit_original_response(embed=discord.Embed(
                title="❌ Error", description=msg, color=0xED4245), view=None)

    @discord.ui.button(label="💬 General Support", style=discord.ButtonStyle.primary,   row=0)
    async def dm_general(self, i, b):    await self._create_from_dm(i, "general")

    @discord.ui.button(label="🚨 Report",          style=discord.ButtonStyle.danger,    row=0)
    async def dm_report(self, i, b):     await self._create_from_dm(i, "report")

    @discord.ui.button(label="💡 Suggestion",      style=discord.ButtonStyle.success,   row=1)
    async def dm_suggestion(self, i, b): await self._create_from_dm(i, "suggestion")

    @discord.ui.button(label="📩 Other",            style=discord.ButtonStyle.secondary, row=1)
    async def dm_other(self, i, b):      await self._create_from_dm(i, "other")

    @discord.ui.button(label="🚫 Cancel", style=discord.ButtonStyle.danger, row=2)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        pending_choices.discard(self.user_id)
        dm_initial_msg.pop(self.user_id, None)
        dm_initial_atts.pop(self.user_id, None)
        dm_origin_guild.pop(self.user_id, None)
        await interaction.response.edit_message(embed=discord.Embed(
            title="Cancelled", description="No ticket was created.", color=0x2F3136), view=None)


# -----------------------------------------------------------------------------
# 4. Mod Panel View
#    Posted inside every ticket thread. Gives staff quick-action buttons.
#    Also survives restarts (timeout=None, persistent custom_ids).
# -----------------------------------------------------------------------------
class ModPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Only staff with access to this ticket category can use the buttons."""
        uid  = ticket_threads.get(interaction.channel.id)
        info = open_tickets.get(uid, {}) if uid else {}
        cat  = info.get("category", "")
        if not can_access_ticket(interaction.user, cat):
            await interaction.response.send_message(
                "🔒 You don't have access to this ticket.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="🔒 Claim",    style=discord.ButtonStyle.secondary, custom_id="mod:claim",    row=0)
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Claim this ticket — renames the thread so others know it's taken."""
        uid = ticket_threads.get(interaction.channel.id)
        if uid and uid in open_tickets:
            open_tickets[uid]["claimed_by"] = interaction.user.id
        try:
            await interaction.channel.edit(name=f"claimed-{interaction.user.name[:18]}")
        except discord.Forbidden:
            pass
        await interaction.response.send_message(embed=discord.Embed(
            description=f"✅ Claimed by {interaction.user.mention}", color=0x57F287))

    @discord.ui.button(label="🎯 Priority", style=discord.ButtonStyle.primary,   custom_id="mod:priority", row=0)
    async def priority(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Open the priority picker for this ticket."""
        await interaction.response.send_message(
            embed=discord.Embed(title="Set Priority", color=0x5865F2),
            view=PriorityPickerView(interaction.channel.id), ephemeral=True)

    @discord.ui.button(label="📝 Note",     style=discord.ButtonStyle.secondary, custom_id="mod:note",     row=0)
    async def note(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Add a private staff note to this ticket (visible in thread only)."""
        await interaction.response.send_modal(NoteModal(interaction.channel.id))

    @discord.ui.button(label="🔴 Close",    style=discord.ButtonStyle.danger,    custom_id="mod:close",    row=0)
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Close this ticket — prompts for a reason, then locks + archives the thread."""
        await interaction.response.send_modal(CloseReasonModal(interaction.channel.id))


# -----------------------------------------------------------------------------
# 5. Priority Picker View  (ephemeral, shown after clicking Priority button)
# -----------------------------------------------------------------------------
class PriorityPickerView(discord.ui.View):
    def __init__(self, thread_id: int):
        super().__init__(timeout=60)
        self.thread_id = thread_id

    async def _set_priority(self, interaction: discord.Interaction, level: str):
        uid = ticket_threads.get(self.thread_id)
        if uid and uid in open_tickets:
            open_tickets[uid]["priority"] = level
        cfg = PRIORITY_LEVELS[level]
        await interaction.response.edit_message(
            embed=discord.Embed(
                title=f"{cfg['emoji']} Priority set to {cfg['label']}",
                color=cfg["color"]), view=None)
        await interaction.channel.send(embed=discord.Embed(
            description=f"{cfg['emoji']} Priority → **{cfg['label']}** set by {interaction.user.mention}",
            color=cfg["color"]))

    @discord.ui.button(label="🟢 Low",    style=discord.ButtonStyle.success)
    async def low(self, i, b):    await self._set_priority(i, "low")

    @discord.ui.button(label="🟡 Medium", style=discord.ButtonStyle.secondary)
    async def medium(self, i, b): await self._set_priority(i, "medium")

    @discord.ui.button(label="🟠 High",   style=discord.ButtonStyle.primary)
    async def high(self, i, b):   await self._set_priority(i, "high")

    @discord.ui.button(label="🔴 Urgent", style=discord.ButtonStyle.danger)
    async def urgent(self, i, b): await self._set_priority(i, "urgent")


# -----------------------------------------------------------------------------
# 6. Note Modal  (staff private notes)
# -----------------------------------------------------------------------------
class NoteModal(discord.ui.Modal, title="Add Private Note"):
    note_text = discord.ui.TextInput(
        label="Note (only visible in this thread)",
        style=discord.TextStyle.paragraph,
        max_length=1000,
        placeholder="Write your private note here…",
    )

    def __init__(self, thread_id: int):
        super().__init__()
        self.thread_id = thread_id

    async def on_submit(self, interaction: discord.Interaction):
        ticket_notes.setdefault(self.thread_id, []).append({
            "mod":  interaction.user.name,
            "note": self.note_text.value,
            "time": fmt_time(datetime.now(timezone.utc)),
        })
        await interaction.response.send_message(embed=discord.Embed(
            title="📝 Note Added",
            description=self.note_text.value,
            color=0xFEE75C,
        ).set_footer(text=f"by {interaction.user.name}"))


# -----------------------------------------------------------------------------
# 7. Close Reason Modal  (shown when staff click the Close button)
# -----------------------------------------------------------------------------
class CloseReasonModal(discord.ui.Modal, title="Close Ticket"):
    reason = discord.ui.TextInput(
        label="Closing reason (sent to the user)",
        style=discord.TextStyle.paragraph,
        max_length=500,
        required=False,
        placeholder="e.g. Issue resolved. Thanks for reaching out!",
    )

    def __init__(self, thread_id: int):
        super().__init__()
        self.thread_id = thread_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await close_ticket(
            thread=interaction.channel,
            closed_by=interaction.user,
            reason=self.reason.value or "No reason provided.",
            followup=interaction.followup,
        )


# -----------------------------------------------------------------------------
# 8. Feedback View  (DM'd to user after their ticket is closed)
# -----------------------------------------------------------------------------
class FeedbackView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=3600)  # 1 hour to rate

    async def _rate(self, interaction: discord.Interaction, stars: int):
        ticket_stats["ratings"].append(stars)
        save_persistent()
        labels = {1: "😞 Poor", 2: "😐 Fair", 3: "🙂 Good", 4: "😊 Great", 5: "🤩 Excellent"}
        await interaction.response.edit_message(embed=discord.Embed(
            title="⭐ Thanks for your feedback!",
            description=f"You rated: **{labels[stars]}** {'⭐' * stars}",
            color=0x57F287), view=None)

    @discord.ui.button(label="⭐",         style=discord.ButtonStyle.secondary)
    async def r1(self, i, b): await self._rate(i, 1)

    @discord.ui.button(label="⭐⭐",       style=discord.ButtonStyle.secondary)
    async def r2(self, i, b): await self._rate(i, 2)

    @discord.ui.button(label="⭐⭐⭐",     style=discord.ButtonStyle.primary)
    async def r3(self, i, b): await self._rate(i, 3)

    @discord.ui.button(label="⭐⭐⭐⭐",   style=discord.ButtonStyle.success)
    async def r4(self, i, b): await self._rate(i, 4)

    @discord.ui.button(label="⭐⭐⭐⭐⭐", style=discord.ButtonStyle.success)
    async def r5(self, i, b): await self._rate(i, 5)


# =============================================================================
#  CORE LOGIC — CREATE TICKET
# =============================================================================
async def create_ticket(
    user: discord.User,
    category: str,
    initial_text: str = "",
    initial_atts: list = None,
    origin_guild_id: int = None,
):
    """
    Opens a new ticket for a user.

    Steps:
      1. Post a starter message in the correct staff channel
      2. Create a thread off that message
      3. Post the ticket info embed + mod panel buttons in the thread
      4. Track the ticket in memory
      5. DM the user a welcome message

    Returns (success: bool, message: str, thread | None)
    """
    try:
        guild    = bot.get_guild(GUILD_ID) or await bot.fetch_guild(GUILD_ID)
        cfg      = TICKET_CATEGORIES[category]
        chan_id  = TICKET_CHANNELS.get(category)

        if not chan_id:
            return False, f"No channel configured for category `{category}`. Check config.py.", None

        parent = bot.get_channel(chan_id) or await bot.fetch_channel(chan_id)
        if not parent:
            return False, "Ticket channel not found. Check TICKET_CHANNELS in config.py.", None

        # Work out which server this ticket is coming from
        origin_id   = origin_guild_id or GUILD_ID
        server_name = get_server_name(origin_id)
        short_srv   = server_name[:15]

        num         = next_ticket_number()
        thread_name = f"{cfg['emoji']} {num} [{short_srv}] {user.name[:16]}"

        # Post a starter message first — Discord requires this to create a thread
        # on a regular text channel (as opposed to a Forum channel).
        starter = await parent.send(
            f"{cfg['emoji']} **Ticket #{num}** — `{user.name}` • opening…"
        )
        thread = await starter.create_thread(
            name=thread_name,
            auto_archive_duration=10080,  # 7 days
        )

        # --- Mod notification embed ---
        role_id     = TICKET_ROLES.get(category)
        team_role   = guild.get_role(role_id) if role_id else None
        mention_str = team_role.mention if team_role else "@here"

        origin_guild = bot.get_guild(origin_id)
        origin_icon  = str(origin_guild.icon.url) if origin_guild and origin_guild.icon else None

        info_embed = discord.Embed(color=cfg["color"])
        info_embed.set_author(
            name=f"📬 New Ticket #{num} from {server_name}",
            icon_url=origin_icon,
        )
        info_embed.add_field(
            name="🌐 From Server",
            value=f"**{server_name}**\n`{origin_id}`",
            inline=True,
        )
        info_embed.add_field(
            name="👤 User",
            value=f"{user.mention}\n`{user.name} · {user.id}`",
            inline=True,
        )
        info_embed.add_field(name="🎫 Category", value=cfg["label"], inline=True)
        info_embed.add_field(
            name="📝 Initial Message",
            value=initial_text or "*(no message)*",
            inline=False,
        )
        info_embed.set_thumbnail(url=user.display_avatar.url)
        info_embed.set_footer(text=f"Ticket #{num} · {fmt_time(datetime.now(timezone.utc))}")

        await thread.send(mention_str, embed=info_embed, view=ModPanelView())

        # Echo the user's first message as plain text so it's easy to read
        if initial_text:
            await thread.send(f"{user.name}: {initial_text}")

        # Forward any attachments from the initial DM message
        if initial_atts:
            for att in initial_atts:
                try:
                    await thread.send(file=await att.to_file())
                except Exception:
                    pass

        # --- Track the ticket ---
        open_tickets[user.id] = {
            "thread":          thread,
            "category":        category,
            "priority":        "low",
            "claimed_by":      None,
            "opened_at":       datetime.now(timezone.utc),
            "origin_guild_id": origin_id,
        }
        ticket_threads[thread.id]  = user.id
        last_activity[thread.id]   = datetime.now(timezone.utc)
        ticket_stats["by_type"][category] += 1
        save_persistent()

        # --- Welcome DM to the user ---
        try:
            dm = user.dm_channel or await user.create_dm()
            await dm.send(
                f"📬 **Your ticket to {cfg['label_dm']} has been opened!**\n"
                f"The team will reply here shortly.\n"
                f"Just send your messages here and they'll be forwarded automatically."
            )
            if initial_text:
                await dm.send(f"**You:** {initial_text}")
        except discord.Forbidden:
            # User has DMs closed — warn staff in thread
            await thread.send(
                f"⚠️ **Cannot DM {user.mention}** — their DMs are closed.\n"
                f"They won't receive replies until they enable DMs from server members."
            )
        except Exception as e:
            print(f"Welcome DM error: {e}")

        print(f"✅ Ticket #{num} ({category}) opened by {user.name} from {server_name}")
        return True, "Ticket created!", thread

    except discord.Forbidden as e:
        print(f"Permission error creating ticket: {e}")
        return False, (
            "Missing permissions in the ticket channel.\n"
            "Make sure the bot has: **Send Messages, Create Public Threads, "
            "Send Messages in Threads, Manage Threads**."
        ), None
    except discord.HTTPException as e:
        print(f"HTTP error creating ticket: {e.status} {e.text}")
        return False, f"Discord API error ({e.status}): {e.text[:150]}", None
    except Exception as e:
        import traceback
        traceback.print_exc()
        return False, f"Unexpected error: {str(e)[:200]}", None


# =============================================================================
#  CORE LOGIC — CLOSE TICKET
# =============================================================================
async def close_ticket(
    thread: discord.Thread,
    closed_by,
    reason: str,
    followup=None,
):
    """
    Closes a ticket:
      1. DMs the user a close notice + feedback prompt
      2. Saves an HTML transcript to the log channel
      3. Removes the ticket from memory
      4. Locks and archives the thread (does NOT delete it)
    """
    uid  = ticket_threads.get(thread.id)
    info = open_tickets.get(uid) if uid else None

    # --- DM the user ---
    if uid:
        try:
            user = bot.get_user(uid) or await bot.fetch_user(uid)
            if user:
                dm        = user.dm_channel or await user.create_dm()
                cat_label = get_thread_label(info["category"]) if info else "Staff"
                close_embed = discord.Embed(
                    title="🔴 Your Ticket Has Been Closed",
                    description=(
                        f"Your ticket with **{cat_label}** has been closed.\n"
                        f"**Reason:** {reason}\n\n"
                        f"Thanks for reaching out! Feel free to open a new ticket anytime."
                    ),
                    color=0x2F3136,
                    timestamp=datetime.now(timezone.utc),
                )
                if info and info["opened_at"]:
                    mins = int(
                        (datetime.now(timezone.utc) - info["opened_at"]).total_seconds() // 60
                    )
                    dur = f"{mins // 60}h {mins % 60}m" if mins >= 60 else f"{mins}m"
                    close_embed.add_field(name="Team",     value=cat_label, inline=True)
                    close_embed.add_field(name="Duration", value=dur,       inline=True)
                close_embed.set_footer(text="How was your experience?")
                await dm.send(embed=close_embed, view=FeedbackView())
        except Exception as e:
            print(f"Close DM error: {e}")

    # --- Save transcript ---
    await save_transcript(thread, uid, reason, closed_by, info)

    # --- Clean up state ---
    open_tickets.pop(uid, None)
    ticket_threads.pop(thread.id, None)
    ticket_notes.pop(thread.id, None)
    last_activity.pop(thread.id, None)
    ticket_stats["closed"] += 1
    save_persistent()

    # --- Post close notice in thread ---
    close_msg = discord.Embed(
        title="🔒 Ticket Closed",
        description=(
            f"Closed by {closed_by.mention}\n"
            f"**Reason:** {reason}\n\n"
            f"*This thread is now locked and archived.*"
        ),
        color=0x2F3136,
    )
    if followup:
        await followup.send(embed=close_msg)
    else:
        await thread.send(embed=close_msg)

    await asyncio.sleep(1)

    # Lock and archive — thread stays visible but nobody can post
    try:
        await thread.edit(locked=True, archived=True)
    except Exception as e:
        print(f"Thread lock/archive error: {e}")


# =============================================================================
#  RELAY — USER DM → STAFF THREAD
# =============================================================================
async def relay_user_to_thread(message: discord.Message):
    """
    Called when a user sends a DM to the bot while they have an open ticket.
    Forwards the message (and any attachments) to the staff thread.
    """
    uid  = message.author.id
    info = open_tickets.get(uid)
    if not info:
        return

    thread = info["thread"]
    last_activity[thread.id] = datetime.now(timezone.utc)

    try:
        sent = False

        if message.content:
            await thread.send(f"{message.author.name}: {message.content}")
            sent = True

        for att in message.attachments:
            try:
                await thread.send(file=await att.to_file())
                sent = True
            except Exception as e:
                print(f"Attachment relay error: {e}")

        # React to confirm the message was received
        if sent:
            try:
                await message.add_reaction("✅")
            except Exception:
                pass

    except Exception as e:
        print(f"relay_user_to_thread error: {e}")
        try:
            await message.channel.send(
                "⚠️ Failed to forward your message. Please try again."
            )
        except Exception:
            pass


# =============================================================================
#  RELAY — STAFF THREAD → USER DM
# =============================================================================
async def relay_thread_to_user(message: discord.Message):
    """
    Called when someone sends a message in a ticket thread.
    If the sender is a staff member (has ticket access), their message
    is forwarded to the user's DMs.

    The DM shows the team name (e.g. "Support Team: Hello!") rather than
    the individual staff member's username.
    """
    uid = ticket_threads.get(message.channel.id)
    if not uid:
        return

    # Don't relay the ticket owner's own messages
    if message.author.id == uid:
        return

    # Verify the sender has staff access for this ticket category
    guild  = bot.get_guild(GUILD_ID)
    member = guild.get_member(message.author.id) if guild else None
    if member is None and guild:
        try:
            member = await guild.fetch_member(message.author.id)
        except Exception:
            member = None

    info = open_tickets.get(uid, {})
    cat  = info.get("category", "")

    if not can_access_ticket(member or message.author, cat):
        return  # Not a staff member, ignore silently

    last_activity[message.channel.id] = datetime.now(timezone.utc)

    try:
        user = bot.get_user(uid) or await bot.fetch_user(uid)
        if not user:
            await message.channel.send("⚠️ Could not find the user.", delete_after=10)
            return

        dm    = user.dm_channel or await user.create_dm()
        label = get_thread_label(cat)  # e.g. "Support Team"

        sent = False

        if message.content:
            await dm.send(f"{label}: {message.content}")
            sent = True

        for att in message.attachments:
            try:
                await dm.send(file=await att.to_file())
                sent = True
            except Exception as e:
                print(f"Attachment relay error: {e}")

        # React to confirm delivery
        if sent:
            try:
                await message.add_reaction("📨")
            except Exception:
                pass

    except discord.Forbidden:
        await message.channel.send(
            "⚠️ Could not DM the user — they may have DMs closed or blocked the bot.",
            delete_after=15,
        )
    except Exception as e:
        print(f"relay_thread_to_user error: {e}")
        await message.channel.send(
            f"⚠️ Relay error: {str(e)[:100]}", delete_after=10
        )


# =============================================================================
#  TRANSCRIPT — Save HTML log when a ticket closes
# =============================================================================
async def save_transcript(thread: discord.Thread, uid, reason: str, closed_by, info):
    """
    Builds an HTML transcript of the ticket conversation and posts it
    to the log channel as an attachment.
    """
    try:
        log_ch = bot.get_channel(LOG_CHANNEL_ID) or await bot.fetch_channel(LOG_CHANNEL_ID)
        if not log_ch:
            return

        cat      = info["category"] if info else "N/A"
        cat_cfg  = TICKET_CATEGORIES.get(cat, {})
        cat_label = cat_cfg.get("label_dm", cat.capitalize())

        # Collect all messages from the thread
        messages = [msg async for msg in thread.history(limit=None, oldest_first=True)]

        def classify(author_id):
            if author_id == uid:         return "user"
            if author_id == bot.user.id: return "bot"
            return "mod"

        def esc(t):
            return str(t).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace("\n","<br>")

        # Build message rows
        rows     = []
        prev_uid = None
        for msg in messages:
            role = classify(msg.author.id)
            ts   = msg.created_at.strftime("%d %b %Y, %H:%M")
            name = cat_label if role == "mod" else msg.author.display_name

            show_header = (msg.author.id != prev_uid)
            prev_uid    = msg.author.id

            att_html = "".join(
                f'<div class="attachment"><img src="{a.url}" alt="{esc(a.filename)}"></div>'
                if any(a.filename.lower().endswith(x) for x in [".png",".jpg",".jpeg",".gif",".webp"])
                else f'<div class="file-att">📎 <a href="{a.url}" target="_blank">{esc(a.filename)}</a></div>'
                for a in msg.attachments
            )

            emb_html = ""
            for emb in msg.embeds:
                parts = []
                if emb.title:       parts.append(f'<div class="emb-title">{esc(emb.title)}</div>')
                if emb.description: parts.append(f'<div class="emb-desc">{esc(emb.description)}</div>')
                for f in emb.fields:
                    parts.append(f'<div class="emb-field"><span class="emb-fn">{esc(f.name)}</span><span class="emb-fv">{esc(f.value)}</span></div>')
                if parts:
                    col = f"#{emb.color.value:06x}" if emb.color else "#5865f2"
                    emb_html += f'<div class="embed" style="border-left-color:{col}">{"".join(parts)}</div>'

            content_html = f'<div class="msg-text">{esc(msg.content)}</div>' if msg.content else ""

            if not content_html and not att_html and not emb_html:
                continue

            header_html = ""
            if show_header:
                bc = {"user":"badge-user","mod":"badge-mod","bot":"badge-bot"}[role]
                nc = {"user":"user-name", "mod":"mod-name", "bot":"bot-name"}[role]
                bd = {"user":"USER","mod":"MOD","bot":"BOT"}[role]
                header_html = (
                    f'<div class="msg-header">'
                    f'<span class="msg-name {nc}">{esc(name)}</span>'
                    f'<span class="badge {bc}">{bd}</span>'
                    f'<span class="msg-ts">{ts}</span>'
                    f'</div>'
                )

            rows.append(
                f'<div class="msg-group {role}">{header_html}{content_html}{att_html}{emb_html}</div>'
            )

        msg_html = "\n".join(rows) or '<div class="empty">No messages recorded.</div>'

        # Notes section
        notes      = ticket_notes.get(thread.id, [])
        notes_html = ""
        if notes:
            nr = "".join(
                f'<div class="note-row">'
                f'<span class="note-mod">{esc(n["mod"])}</span>'
                f'<span class="note-time">{n["time"]}</span>'
                f'<div class="note-text">{esc(n["note"])}</div>'
                f'</div>'
                for n in notes
            )
            notes_html = f'<section><h2>📝 Staff Notes</h2><div class="notes-list">{nr}</div></section>'

        # Stats
        pr_cfg      = PRIORITY_LEVELS.get(info["priority"] if info else "low", PRIORITY_LEVELS["low"])
        opened_str  = fmt_time(info["opened_at"]) if info and info["opened_at"] else "N/A"
        origin_gid  = info.get("origin_guild_id") if info else None
        origin_name = get_server_name(origin_gid) if origin_gid else "N/A"
        origin_g    = bot.get_guild(origin_gid) if origin_gid else None
        origin_icon = str(origin_g.icon.url) if origin_g and origin_g.icon else None

        if info and info["opened_at"]:
            mins = int((datetime.now(timezone.utc) - info["opened_at"]).total_seconds() // 60)
            dur  = f"{mins // 60}h {mins % 60}m" if mins >= 60 else f"{mins}m"
        else:
            dur = "N/A"

        total_msgs = len([m for m in messages if not m.author.bot])
        user_msgs  = len([m for m in messages if m.author.id == uid])
        mod_msgs   = total_msgs - user_msgs

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Transcript — {esc(thread.name)}</title>
<style>
:root{{--bg:#1e1f22;--bg2:#2b2d31;--bg3:#313338;--border:#3a3c40;--text:#dbdee1;--muted:#949ba4;--user:#5865f2;--mod:#57f287;--bot:#eb459e}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Segoe UI',sans-serif;background:var(--bg);color:var(--text);font-size:15px;line-height:1.5}}
.header{{background:var(--bg2);border-bottom:1px solid var(--border);padding:20px 32px;display:flex;align-items:center;gap:16px}}
.header-icon{{font-size:2rem}}.header-title{{font-size:1.25rem;font-weight:700;color:#fff}}
.header-sub{{font-size:.85rem;color:var(--muted);margin-top:2px}}
.stats{{background:var(--bg3);border-bottom:1px solid var(--border);padding:12px 32px;display:flex;flex-wrap:wrap;gap:24px}}
.stat{{display:flex;flex-direction:column}}
.stat-label{{font-size:.7rem;font-weight:600;text-transform:uppercase;letter-spacing:.05em;color:var(--muted)}}
.stat-value{{font-size:.95rem;font-weight:600;color:var(--text);margin-top:2px}}
.container{{max-width:900px;margin:0 auto;padding:24px 32px}}
section{{margin-bottom:32px}}
h2{{font-size:.8rem;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);border-bottom:1px solid var(--border);padding-bottom:8px;margin-bottom:16px}}
.msg-group{{padding:2px 0}}
.msg-header{{display:flex;align-items:center;gap:8px;margin-bottom:2px;margin-top:14px}}
.msg-name{{font-weight:600;font-size:.95rem}}
.user-name{{color:var(--user)}}.mod-name{{color:var(--mod)}}.bot-name{{color:var(--bot)}}
.badge{{font-size:.62rem;font-weight:700;text-transform:uppercase;padding:1px 5px;border-radius:3px}}
.badge-user{{background:rgba(88,101,242,.25);color:#8b9cf4}}
.badge-mod{{background:rgba(87,242,135,.2);color:#57f287}}
.badge-bot{{background:rgba(235,69,158,.2);color:#eb459e}}
.msg-ts{{font-size:.75rem;color:var(--muted)}}
.msg-text{{font-size:.95rem;color:var(--text);word-break:break-word}}
.attachment img{{max-width:400px;max-height:300px;border-radius:8px;margin-top:6px;display:block;border:1px solid var(--border)}}
.file-att{{margin-top:4px;font-size:.9rem}}.file-att a{{color:var(--user);text-decoration:none}}
.embed{{background:var(--bg2);border-left:4px solid #5865f2;border-radius:0 4px 4px 0;padding:8px 12px;margin-top:4px;font-size:.9rem}}
.emb-title{{font-weight:700;color:#fff;margin-bottom:4px}}.emb-desc{{color:var(--text);white-space:pre-wrap}}
.emb-fn{{font-weight:600;font-size:.85rem;display:block;color:var(--muted)}}.emb-fv{{color:var(--text)}}
.notes-list{{display:flex;flex-direction:column;gap:8px}}
.note-row{{background:var(--bg2);border-left:3px solid #fee75c;border-radius:0 6px 6px 0;padding:10px 14px}}
.note-mod{{font-weight:700;color:#fee75c;font-size:.9rem}}
.note-time{{font-size:.75rem;color:var(--muted);margin-left:8px}}
.note-text{{margin-top:4px;font-size:.9rem;color:var(--text)}}
.empty{{color:var(--muted);font-style:italic;padding:12px 0}}
footer{{text-align:center;font-size:.75rem;color:var(--muted);border-top:1px solid var(--border);padding:20px 32px;margin-top:8px}}
</style>
</head>
<body>
<div class="header">
  <div class="header-icon">{esc(cat_cfg.get('emoji','📋'))}</div>
  <div>
    <div class="header-title">{esc(thread.name)}</div>
    <div class="header-sub">ModMail Transcript</div>
  </div>
</div>
<div class="stats">
  <div class="stat"><span class="stat-label">User</span><span class="stat-value"><@{uid}></span></div>
  <div class="stat"><span class="stat-label">From Server</span><span class="stat-value">{esc(origin_name)}</span></div>
  <div class="stat"><span class="stat-label">Team</span><span class="stat-value">{esc(cat_label)}</span></div>
  <div class="stat"><span class="stat-label">Priority</span><span class="stat-value">{pr_cfg['emoji']} {pr_cfg['label']}</span></div>
  <div class="stat"><span class="stat-label">Opened</span><span class="stat-value">{opened_str}</span></div>
  <div class="stat"><span class="stat-label">Duration</span><span class="stat-value">{dur}</span></div>
  <div class="stat"><span class="stat-label">Closed by</span><span class="stat-value">{esc(closed_by.name)}</span></div>
  <div class="stat"><span class="stat-label">Reason</span><span class="stat-value">{esc(reason)}</span></div>
  <div class="stat"><span class="stat-label">Messages</span><span class="stat-value">{total_msgs} total · {user_msgs} user · {mod_msgs} staff</span></div>
</div>
<div class="container">
  <section><h2>💬 Conversation</h2>{msg_html}</section>
  {notes_html}
</div>
<footer>ModMail · Generated {fmt_time(datetime.now(timezone.utc))} · {esc(thread.name)}</footer>
</body>
</html>"""

        buf  = BytesIO(html.encode("utf-8"))
        file = discord.File(buf, filename=f"transcript-{thread.name}.html")

        log_embed = discord.Embed(
            title=f"🔒 Closed — {thread.name}",
            color=0x2F3136,
            timestamp=datetime.now(timezone.utc),
        )
        log_embed.add_field(name="🌐 From Server", value=f"**{origin_name}**\n`{origin_gid}`", inline=True)
        log_embed.add_field(name="👤 User",        value=f"<@{uid}> · `{uid}`",               inline=True)
        log_embed.add_field(name="🏷️ Team",        value=cat_label,                           inline=True)
        log_embed.add_field(name="🔴 Closed by",   value=closed_by.mention,                   inline=True)
        log_embed.add_field(name="🎯 Priority",    value=f"{pr_cfg['emoji']} {pr_cfg['label']}", inline=True)
        log_embed.add_field(name="⏱️ Duration",    value=dur,                                 inline=True)
        log_embed.add_field(name="📝 Reason",      value=reason,                              inline=False)
        log_embed.add_field(name="💬 Messages",
            value=f"{total_msgs} total ({user_msgs} user · {mod_msgs} staff)", inline=True)
        if notes:
            log_embed.add_field(
                name=f"📝 Notes ({len(notes)})",
                value="\n".join(f"**{n['mod']}**: {n['note']}" for n in notes)[:1024],
                inline=False,
            )
        if origin_icon:
            log_embed.set_thumbnail(url=origin_icon)
        log_embed.set_footer(text="Open the attached HTML file for the full conversation")

        await log_ch.send(embed=log_embed, file=file)
        print(f"✅ Transcript saved: {thread.name}")

    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"Transcript error: {e}")


# =============================================================================
#  BOT EVENTS
# =============================================================================

@bot.event
async def on_ready():
    """Called when the bot connects to Discord."""
    load_persistent()

    # Register persistent views so buttons still work after a restart
    bot.add_view(TicketPanelView())
    bot.add_view(ModPanelView())

    auto_close_task.start()

    try:
        synced = await bot.tree.sync()
        print(f"✅ Synced {len(synced)} slash commands")
    except Exception as e:
        print(f"Slash sync error: {e}")

    await bot.change_presence(
        status=discord.Status.online,
        activity=discord.CustomActivity(name="📬 Open a ticket in the server!"),
    )
    print(f"✅ {bot.user} is online · {len(bot.guilds)} guild(s)")


@bot.event
async def on_command_error(ctx, error):
    """Handle prefix command errors."""
    if isinstance(error, commands.CheckFailure):
        await ctx.send(embed=discord.Embed(
            title="🔒 No Permission",
            description="You don't have permission to use this command.",
            color=0xED4245))
    elif isinstance(error, commands.CommandNotFound):
        pass  # Silently ignore unknown commands
    else:
        raise error


@bot.event
async def on_message(message: discord.Message):
    """
    Main message handler.

    DM messages:
      - If user has open ticket → relay to staff thread
      - If user has no ticket   → show the ticket type menu

    Thread messages:
      - If from staff → relay to user's DMs
    """
    if message.author.bot:
        return

    # --- Incoming DM ---
    if isinstance(message.channel, discord.DMChannel):
        uid = message.author.id

        if uid in banned_users:
            await message.author.send(embed=discord.Embed(
                title="🚫 Banned",
                description="You are not allowed to open tickets.",
                color=0xED4245))
            return

        # Already has an open ticket — relay this message to the thread
        if uid in open_tickets:
            await relay_user_to_thread(message)
            return

        # Cooldown check
        secs = check_cooldown(uid)
        if secs:
            await message.author.send(embed=discord.Embed(
                title="⏳ Please Wait",
                description=f"You can open a new ticket in **{secs}s**.",
                color=0xED4245))
            return

        # New DM — show the category picker
        if uid not in pending_choices:
            dm_initial_msg[uid]  = message.content or ""
            dm_initial_atts[uid] = list(message.attachments)

            # Try to detect which server the user is from
            origin_gid = None
            for g in bot.guilds:
                if g.id != GUILD_ID and g.get_member(uid):
                    origin_gid = g.id
                    break
            if origin_gid is None:
                central = bot.get_guild(GUILD_ID)
                if central and central.get_member(uid):
                    origin_gid = GUILD_ID
            dm_origin_guild[uid] = origin_gid

            pending_choices.add(uid)
            cooldowns[uid] = datetime.now(timezone.utc)

            await message.author.send(
                embed=discord.Embed(
                    title="📬 Support — Choose a Category",
                    description=(
                        "Hi! What can we help you with?\n\n"
                        "💬 **General Support** — Questions and general help\n"
                        "🚨 **Report** — Report a user or issue\n"
                        "💡 **Suggestion** — Share an idea or feedback\n"
                        "📩 **Other** — Anything that doesn't fit above\n\n"
                        "💡 *You can also open tickets from the **#support** channel in the server.*"
                    ),
                    color=0x5865F2,
                ).set_footer(text="Select a category below · expires in 10 minutes"),
                view=DMCategoryView(uid),
            )

    # --- Message in a ticket thread ---
    elif isinstance(message.channel, discord.Thread):
        await relay_thread_to_user(message)

    await bot.process_commands(message)


# =============================================================================
#  AUTO-CLOSE TASK
#  Runs every 30 minutes and closes tickets that have been inactive
#  for longer than AUTO_CLOSE_HOURS (set in config.py, 0 = disabled).
# =============================================================================
@tasks.loop(minutes=30)
async def auto_close_task():
    if not AUTO_CLOSE_HOURS:
        return

    cutoff = timedelta(hours=AUTO_CLOSE_HOURS)
    now    = datetime.now(timezone.utc)

    for tid in list(last_activity.keys()):
        if now - last_activity[tid] > cutoff:
            try:
                thread  = bot.get_channel(tid)
                guild   = bot.get_guild(GUILD_ID)
                bot_mem = guild.get_member(bot.user.id) if guild else bot.user
                if thread:
                    await thread.send(embed=discord.Embed(
                        title="⏰ Auto-Closing",
                        description=(
                            f"This ticket has been inactive for **{AUTO_CLOSE_HOURS}h** "
                            f"and is being closed automatically."
                        ),
                        color=0xFFA500))
                    await close_ticket(thread, bot_mem, "Auto-closed due to inactivity.")
            except Exception as e:
                print(f"Auto-close error: {e}")


# =============================================================================
#  PERMISSION DECORATORS
# =============================================================================

def mod_only():
    """Prefix command check — user must have staff access to at least one category."""
    async def predicate(ctx):
        return can_access_any(ctx.author)
    return commands.check(predicate)


def is_mod_or_admin():
    """Slash command check — same as above."""
    async def predicate(interaction: discord.Interaction) -> bool:
        return can_access_any(interaction.user)
    return discord.app_commands.check(predicate)


async def on_app_command_error(interaction: discord.Interaction, error):
    """Global slash command error handler."""
    if isinstance(error, discord.app_commands.CheckFailure):
        try:
            await interaction.response.send_message(embed=discord.Embed(
                title="🔒 No Permission",
                description="You don't have access to this command.",
                color=0xED4245), ephemeral=True)
        except Exception:
            pass
    else:
        raise error

bot.tree.on_error = on_app_command_error


# =============================================================================
#  STAFF COMMANDS  (both prefix  !cmd  and slash  /cmd  versions)
# =============================================================================

# --- /setup  !setup ---
@bot.command(name="setup")
@mod_only()
async def cmd_setup(ctx: commands.Context):
    """Post the ticket panel in this channel."""
    e = _build_panel_embed()
    await ctx.message.delete()
    await ctx.send(embed=e, view=TicketPanelView())

@bot.tree.command(name="setup", description="Post the ticket panel in this channel")
@is_mod_or_admin()
async def slash_setup(interaction: discord.Interaction):
    # Respond immediately to avoid the "did not respond" timeout
    await interaction.response.send_message("✅ Panel posted!", ephemeral=True)
    await interaction.channel.send(embed=_build_panel_embed(), view=TicketPanelView())

def _build_panel_embed() -> discord.Embed:
    e = discord.Embed(
        title="📬 Support",
        description=(
            "Click a button below to open a ticket.\n\n"
            "💬 **General Support** — Questions and general help\n"
            "🚨 **Report** — Report a user or issue\n"
            "💡 **Suggestion** — Share an idea or feedback\n"
            "📩 **Other** — Anything else\n\n"
            "*After clicking, check your DMs — that's where you'll chat with the team.*"
        ),
        color=0x5865F2,
    )
    e.set_footer(text="ModMail · Click below to begin")
    return e


# --- /close  !close ---
@bot.command(name="close")
@mod_only()
async def cmd_close(ctx: commands.Context, *, reason: str = "Closed by staff."):
    """Close the current ticket thread."""
    if not isinstance(ctx.channel, discord.Thread) or ctx.channel.id not in ticket_threads:
        await ctx.send(embed=discord.Embed(
            description="❌ Run this inside a ticket thread.", color=0xED4245))
        return
    await close_ticket(ctx.channel, ctx.author, reason)

@bot.tree.command(name="close", description="Close the current ticket")
@is_mod_or_admin()
@discord.app_commands.describe(reason="Reason for closing")
async def slash_close(interaction: discord.Interaction, reason: str = "Closed by staff."):
    if not isinstance(interaction.channel, discord.Thread) or interaction.channel.id not in ticket_threads:
        await interaction.response.send_message(embed=discord.Embed(
            description="❌ Run this inside a ticket thread.", color=0xED4245), ephemeral=True)
        return
    await interaction.response.send_modal(CloseReasonModal(interaction.channel.id))


# --- /claim  !claim ---
@bot.command(name="claim")
@mod_only()
async def cmd_claim(ctx: commands.Context):
    """Claim this ticket — renames the thread to show it's taken."""
    if not isinstance(ctx.channel, discord.Thread) or ctx.channel.id not in ticket_threads:
        await ctx.send(embed=discord.Embed(
            description="❌ Run this inside a ticket thread.", color=0xED4245))
        return
    uid = ticket_threads[ctx.channel.id]
    if uid in open_tickets:
        open_tickets[uid]["claimed_by"] = ctx.author.id
    try:
        await ctx.channel.edit(name=f"claimed-{ctx.author.name[:18]}")
    except discord.Forbidden:
        pass
    await ctx.send(embed=discord.Embed(
        description=f"✅ Claimed by {ctx.author.mention}", color=0x57F287))

@bot.tree.command(name="claim", description="Claim this ticket")
@is_mod_or_admin()
async def slash_claim(interaction: discord.Interaction):
    if not isinstance(interaction.channel, discord.Thread) or interaction.channel.id not in ticket_threads:
        await interaction.response.send_message(embed=discord.Embed(
            description="❌ Run this inside a ticket thread.", color=0xED4245), ephemeral=True)
        return
    uid = ticket_threads[interaction.channel.id]
    if uid in open_tickets:
        open_tickets[uid]["claimed_by"] = interaction.user.id
    try:
        await interaction.channel.edit(name=f"claimed-{interaction.user.name[:18]}")
    except discord.Forbidden:
        pass
    await interaction.response.send_message(embed=discord.Embed(
        description=f"✅ Claimed by {interaction.user.mention}", color=0x57F287))


# --- /note  !note  /notes  !notes ---
@bot.command(name="note")
@mod_only()
async def cmd_note(ctx: commands.Context, *, note: str):
    """Add a private staff note to this ticket."""
    if ctx.channel.id not in ticket_threads:
        await ctx.send(embed=discord.Embed(
            description="❌ Run this inside a ticket thread.", color=0xED4245))
        return
    ticket_notes.setdefault(ctx.channel.id, []).append(
        {"mod": ctx.author.name, "note": note, "time": fmt_time(datetime.now(timezone.utc))})
    await ctx.send(embed=discord.Embed(
        title="📝 Note Added", description=note, color=0xFEE75C)
        .set_footer(text=f"by {ctx.author.name}"))

@bot.tree.command(name="note", description="Add a private note to this ticket")
@is_mod_or_admin()
@discord.app_commands.describe(note="The note text")
async def slash_note(interaction: discord.Interaction, note: str):
    if interaction.channel.id not in ticket_threads:
        await interaction.response.send_message(embed=discord.Embed(
            description="❌ Run this inside a ticket thread.", color=0xED4245), ephemeral=True)
        return
    ticket_notes.setdefault(interaction.channel.id, []).append(
        {"mod": interaction.user.name, "note": note, "time": fmt_time(datetime.now(timezone.utc))})
    await interaction.response.send_message(embed=discord.Embed(
        title="📝 Note Added", description=note, color=0xFEE75C)
        .set_footer(text=f"by {interaction.user.name}"))

@bot.command(name="notes")
@mod_only()
async def cmd_notes(ctx: commands.Context):
    """View all staff notes for this ticket."""
    notes = ticket_notes.get(ctx.channel.id, [])
    if not notes:
        await ctx.send(embed=discord.Embed(description="No notes yet.", color=0x5865F2))
        return
    e = discord.Embed(title="📝 Staff Notes", color=0xFEE75C)
    for n in notes:
        e.add_field(name=f"{n['mod']} · {n['time']}", value=n["note"], inline=False)
    await ctx.send(embed=e)

@bot.tree.command(name="notes", description="View all notes for this ticket")
@is_mod_or_admin()
async def slash_notes(interaction: discord.Interaction):
    notes = ticket_notes.get(interaction.channel.id, [])
    if not notes:
        await interaction.response.send_message(embed=discord.Embed(
            description="No notes yet.", color=0x5865F2), ephemeral=True)
        return
    e = discord.Embed(title="📝 Staff Notes", color=0xFEE75C)
    for n in notes:
        e.add_field(name=f"{n['mod']} · {n['time']}", value=n["note"], inline=False)
    await interaction.response.send_message(embed=e, ephemeral=True)


# --- /priority  !priority ---
@bot.command(name="priority")
@mod_only()
async def cmd_priority(ctx: commands.Context, level: str):
    """Set priority: low / medium / high / urgent"""
    level = level.lower()
    if level not in PRIORITY_LEVELS:
        await ctx.send(embed=discord.Embed(
            description="Valid levels: `low` `medium` `high` `urgent`", color=0xED4245))
        return
    uid = ticket_threads.get(ctx.channel.id)
    if uid and uid in open_tickets:
        open_tickets[uid]["priority"] = level
    cfg = PRIORITY_LEVELS[level]
    await ctx.send(embed=discord.Embed(
        description=f"{cfg['emoji']} Priority → **{cfg['label']}**", color=cfg["color"]))

@bot.tree.command(name="priority", description="Set ticket priority")
@is_mod_or_admin()
@discord.app_commands.describe(level="Priority level")
@discord.app_commands.choices(level=[
    discord.app_commands.Choice(name="🟢 Low",    value="low"),
    discord.app_commands.Choice(name="🟡 Medium", value="medium"),
    discord.app_commands.Choice(name="🟠 High",   value="high"),
    discord.app_commands.Choice(name="🔴 Urgent", value="urgent"),
])
async def slash_priority(interaction: discord.Interaction, level: str):
    uid = ticket_threads.get(interaction.channel.id)
    if uid and uid in open_tickets:
        open_tickets[uid]["priority"] = level
    cfg = PRIORITY_LEVELS[level]
    await interaction.response.send_message(embed=discord.Embed(
        description=f"{cfg['emoji']} Priority → **{cfg['label']}**", color=cfg["color"]))


# --- /ban  !ban  /unban  !unban ---
@bot.command(name="ban")
@mod_only()
async def cmd_ban(ctx: commands.Context, user: discord.User, *, reason: str = "No reason provided."):
    """Ban a user from opening tickets."""
    banned_users.add(user.id)
    save_persistent()
    await ctx.send(embed=discord.Embed(
        title="🚫 Banned", description=f"{user.mention}\n**Reason:** {reason}", color=0xED4245))
    try:
        await user.send(embed=discord.Embed(
            title="🚫 Banned from ModMail",
            description=f"You've been banned from opening tickets.\n**Reason:** {reason}",
            color=0xED4245))
    except discord.Forbidden:
        pass

@bot.tree.command(name="ban", description="Ban a user from opening tickets")
@is_mod_or_admin()
@discord.app_commands.describe(user="User to ban", reason="Reason for the ban")
async def slash_ban(interaction: discord.Interaction, user: discord.User, reason: str = "No reason provided."):
    banned_users.add(user.id)
    save_persistent()
    await interaction.response.send_message(embed=discord.Embed(
        title="🚫 Banned", description=f"{user.mention}\n**Reason:** {reason}", color=0xED4245))
    try:
        await user.send(embed=discord.Embed(
            title="🚫 Banned from ModMail",
            description=f"You've been banned from opening tickets.\n**Reason:** {reason}",
            color=0xED4245))
    except discord.Forbidden:
        pass

@bot.command(name="unban")
@mod_only()
async def cmd_unban(ctx: commands.Context, user: discord.User):
    """Unban a user so they can open tickets again."""
    banned_users.discard(user.id)
    save_persistent()
    await ctx.send(embed=discord.Embed(
        title="✅ Unbanned",
        description=f"{user.mention} can open tickets again.",
        color=0x57F287))

@bot.tree.command(name="unban", description="Unban a user from tickets")
@is_mod_or_admin()
@discord.app_commands.describe(user="User to unban")
async def slash_unban(interaction: discord.Interaction, user: discord.User):
    banned_users.discard(user.id)
    save_persistent()
    await interaction.response.send_message(embed=discord.Embed(
        title="✅ Unbanned",
        description=f"{user.mention} can open tickets again.",
        color=0x57F287))


# --- /stats  !stats ---
def _build_stats_embed() -> discord.Embed:
    ratings = ticket_stats["ratings"]
    avg = (sum(ratings) / len(ratings)) if ratings else 0
    e = discord.Embed(title="📊 Statistics", color=0x5865F2, timestamp=datetime.now(timezone.utc))
    e.add_field(name="🎫 Total Tickets", value=ticket_stats["total"],  inline=True)
    e.add_field(name="✅ Closed",        value=ticket_stats["closed"], inline=True)
    e.add_field(name="🔓 Open Now",      value=len(open_tickets),      inline=True)
    e.add_field(name="⭐ Avg Rating",    value=f"{avg:.1f}/5 ({len(ratings)} ratings)", inline=True)
    e.add_field(name="🚫 Banned Users",  value=len(banned_users),      inline=True)
    e.add_field(name="⏱️ Latency",      value=f"{bot.latency*1000:.1f}ms", inline=True)
    e.add_field(name="📂 By Category",
        value="\n".join(
            f"{TICKET_CATEGORIES[t]['label']}: **{v}**"
            for t, v in ticket_stats["by_type"].items()
        ),
        inline=False)
    return e

@bot.command(name="stats")
@mod_only()
async def cmd_stats(ctx): await ctx.send(embed=_build_stats_embed())

@bot.tree.command(name="stats", description="View ticket statistics")
@is_mod_or_admin()
async def slash_stats(interaction: discord.Interaction):
    await interaction.response.send_message(embed=_build_stats_embed(), ephemeral=True)


# --- /announce  !announce ---
async def _send_announcement(text: str) -> int:
    """DM all users with open tickets. Returns the count of successful sends."""
    sent = 0
    for uid in list(open_tickets.keys()):
        try:
            user = bot.get_user(uid) or await bot.fetch_user(uid)
            if user:
                dm = user.dm_channel or await user.create_dm()
                await dm.send(embed=discord.Embed(
                    title="📣 Announcement",
                    description=text,
                    color=0xFEE75C))
                sent += 1
        except Exception:
            pass
    return sent

@bot.command(name="announce")
@mod_only()
async def cmd_announce(ctx: commands.Context, *, msg: str):
    """DM an announcement to all users with open tickets."""
    sent = await _send_announcement(msg)
    await ctx.send(embed=discord.Embed(
        description=f"📣 Sent to **{sent}** user(s).", color=0x57F287))

@bot.tree.command(name="announce", description="DM all open-ticket users an announcement")
@is_mod_or_admin()
@discord.app_commands.describe(message="The announcement text")
async def slash_announce(interaction: discord.Interaction, message: str):
    await interaction.response.defer(ephemeral=True)
    sent = await _send_announcement(message)
    await interaction.followup.send(embed=discord.Embed(
        description=f"📣 Sent to **{sent}** user(s).", color=0x57F287))


# --- /status  !status ---
@bot.command(name="status")
async def cmd_status(ctx):
    """Show bot status."""
    await ctx.send(embed=discord.Embed(
        title="✅ Bot Status", color=0x57F287, timestamp=datetime.now(timezone.utc))
        .add_field(name="🤖 Bot",   value=str(bot.user),               inline=True)
        .add_field(name="🎫 Open",  value=len(open_tickets),            inline=True)
        .add_field(name="⏱️ Ping", value=f"{bot.latency*1000:.1f}ms", inline=True))

@bot.tree.command(name="status", description="Check bot status")
async def slash_status(interaction: discord.Interaction):
    await interaction.response.send_message(embed=discord.Embed(
        title="✅ Bot Status", color=0x57F287, timestamp=datetime.now(timezone.utc))
        .add_field(name="🤖 Bot",   value=str(bot.user),               inline=True)
        .add_field(name="🎫 Open",  value=len(open_tickets),            inline=True)
        .add_field(name="⏱️ Ping", value=f"{bot.latency*1000:.1f}ms", inline=True),
        ephemeral=True)


# --- /help  !help ---
@bot.command(name="help")
async def cmd_help(ctx: commands.Context):
    is_staff = can_access_any(ctx.author) if isinstance(ctx.author, discord.Member) else False
    await ctx.send(embed=_build_help_embed(is_staff))

@bot.tree.command(name="help", description="Show bot help")
async def slash_help(interaction: discord.Interaction):
    is_staff = can_access_any(interaction.user) if isinstance(interaction.user, discord.Member) else False
    await interaction.response.send_message(embed=_build_help_embed(is_staff), ephemeral=True)

def _build_help_embed(is_staff: bool) -> discord.Embed:
    e = discord.Embed(title="📖 ModMail Help", color=0x5865F2)
    e.add_field(name="For Members", value=(
        "**Two ways to open a ticket:**\n"
        "• Click a button in the **#support** channel\n"
        "• Or **DM this bot** directly\n\n"
        "📬 Once your ticket is open, all conversation happens in your **DMs with this bot**."
    ), inline=False)
    if is_staff:
        e.add_field(name="Staff Commands", value=(
            "`/setup` — Post the ticket panel\n"
            "`/claim` — Claim a ticket thread\n"
            "`/close [reason]` — Close and archive a ticket\n"
            "`/note <text>` — Add a private staff note\n"
            "`/notes` — View notes for this ticket\n"
            "`/priority <level>` — Set ticket priority\n"
            "`/ban <user>` — Ban a user from tickets\n"
            "`/unban <user>` — Remove a ban\n"
            "`/stats` — View ticket statistics\n"
            "`/announce <msg>` — DM all open-ticket users\n"
            "`/status` — Check bot status\n"
            "\nAll commands also work with `!` prefix."
        ), inline=False)
    return e


# =============================================================================
#  START
# =============================================================================
if __name__ == "__main__":
    try:
        bot.run(TOKEN)
    except KeyboardInterrupt:
        print("Shutting down.")
    except Exception as e:
        print(f"Fatal error: {e}")
