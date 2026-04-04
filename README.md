# ModMail Bot

A Discord modmail bot. Users open tickets via buttons or DM.
All conversation happens through the bot's DMs — staff reply in a private thread.

---

## Files

| File | Purpose |
|---|---|
| `bot.py` | Main bot code |
| `config.py.example` | Config template — copy to `config.py` and fill in |
| `requirements.txt` | Python dependencies |
| `discloud.config` | Discloud hosting config |

---

## Setup Steps

### 1. Create the Discord Bot
1. Go to https://discord.com/developers/applications
2. Click **New Application**
3. Go to **Bot** → **Reset Token** → copy the token
4. Under **Privileged Gateway Intents**, enable:
   - **Server Members Intent**
   - **Message Content Intent**
5. Go to **OAuth2 → URL Generator**
   - Scopes: `bot`, `applications.commands`
   - Permissions: `Send Messages`, `Create Public Threads`,
     `Send Messages in Threads`, `Manage Threads`, `Read Message History`,
     `Add Reactions`, `Embed Links`, `Attach Files`
6. Open the generated URL to invite the bot to your server

---

### 2. Set Up Your Server

Create **one text channel per ticket category** in your central server.
Recommended names: `#general-tickets`, `#report-tickets`, etc.

For each channel, set permissions:
- `@everyone` → **deny** View Channel
- Your staff role → **allow** View Channel, Send Messages, Create Public Threads,
  Send Messages in Threads, Manage Threads

Also create a `#ticket-logs` channel for transcripts (staff-only).

---

### 3. Configure the Bot

```bash
cp config.py.example config.py
```

Open `config.py` and fill in every value:
Note:To find the Guild_ID you have to have Developer Mode On in discord :) Just searh on youtube https://www.youtube.com/results?search_query=how+to+turn+developer+mode+on+discord
| Setting | What to put |
|---|---|
| `TOKEN` | Your bot token from step 1 |
| `GUILD_ID` | Right-click your server → Copy Server ID |
| `LOG_CHANNEL_ID` | Right-click `#ticket-logs` → Copy Channel ID |
| `ADMIN_ROLE_ID` | Right-click your admin role → Copy Role ID |
| `TICKET_CHANNELS` | Channel ID for each category |
| `TICKET_ROLES` | Role ID that handles each category |

**How to get IDs:** Discord Settings → Advanced → enable **Developer Mode**,
then right-click anything to copy its ID.

---

### 4. Customise Categories (optional)

To change the ticket categories, edit `TICKET_CATEGORIES` in `bot.py`.
Then update `TICKET_CHANNELS` and `TICKET_ROLES` in `config.py` to match.

```python
# Example: rename "general" to "help"
TICKET_CATEGORIES = {
    "help": {
        "label":    "🙋 Get Help",
        "emoji":    "🙋",
        "color":    0x5865F2,
        "label_dm": "Support Team",   # shown to user in DMs
    },
    ...
}
```

Also update the panel embed text in `_build_panel_embed()` and
`_build_help_embed()` at the bottom of `bot.py` to match.

---

### 5. Run the Bot (Testing Phase)

**Locally:**
```bash
pip install -r requirements.txt
python bot.py
```
### 6. Host the Discord Bot 
Hosting of a discord bot can be offered by many Services from web hosting services provided by Amazon Web Services (AWS) To Your own Computer/server with docker.
As from the File uploaded, this bot was hosted in Discloud Website/bot hosting which I recommend as they offer quality support and is cheap and reliable 
https://discloud.com/ 
https://docs.discloud.com/en

**On AWS:**
-https://aws.amazon.com/
  Check youtube or Aws website 


  **Self-Hosting:**
I suggest you use Hostinger for your convenience
https://www.youtube.com/watch?v=68aslvcWZ0M

**On Discloud:**
- Make sure `config.py` is included in your zip (not just the example)
- Zip all files flat (no subfolders):
  ```
  bot.py
  config.py
  requirements.txt
  discloud.config
  ```
- Upload the zip to Discloud
 
---

### 7. Post the Ticket Panel

In your support channel, run:
```
/setup
```

or

```
!setup
```

A panel with buttons will appear. Users click a button, fill in a short form,
and their ticket opens. All further conversation happens in DMs with the bot.

---

## How It Works

```
User clicks button
      ↓
Fills in description (modal)
      ↓
Bot creates thread in the right staff channel
Bot DMs the user: "Your ticket is open, reply here"
      ↓
User sends DMs → bot forwards to thread  ✅ reaction confirms
Staff replies in thread → bot forwards to user DM  📨 reaction confirms
      ↓
Staff clicks Close → thread locks, transcript saved, user gets feedback prompt
```

---

## Commands

| Command | Description |
|---|---|
| `/setup` | Post the ticket panel |
| `/close [reason]` | Close and archive the current ticket |
| `/claim` | Claim a ticket (renames thread) |
| `/note <text>` | Add a private staff note |
| `/notes` | View all notes for this ticket |
| `/priority <level>` | Set priority: low / medium / high / urgent |
| `/ban <user>` | Ban a user from opening tickets |
| `/unban <user>` | Remove a ban |
| `/stats` | View ticket statistics |
| `/announce <msg>` | DM all users with open tickets |
| `/status` | Check bot status |

All commands also work with `!` prefix.

---

## Multi-Server Setup

The bot can run in multiple servers but route all tickets to one central server.

1. Invite the bot to each satellite server
2. Run `/setup` in each server's support channel
3. Optionally add server names to `SATELLITE_SERVERS` in `config.py`
   as a fallback (the bot reads actual names from Discord automatically)

Each ticket thread will show which server it came from.
