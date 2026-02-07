import discord
import aiohttp
import os
import threading
import asyncio
from urllib.parse import quote
from discord.ext import commands, tasks
from discord.ui import View, Modal, TextInput, Select
from flask import Flask

from config import *
from database import load_data, save_data, init_db

# ------------------ BOT ------------------

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ------------------ VERIFICACIÓN ------------------

PENDING_VERIFICATIONS = {}
VERIFICATION_ICON_ID = 25

# ------------------ RIOT API ------------------

async def riot_get(url):
    headers = {"X-Riot-Token": RIOT_API_KEY}
    async with aiohttp.ClientSession() as s:
        async with s.get(url, headers=headers) as r:
            if r.status != 200:
                print("Riot API error:", r.status, url)
                return None
            return await r.json()

async def validate_riot_id(name, tag, region):
    _, routing, _ = REGIONS[region]
    return await riot_get(
        f"https://{routing}.api.riotgames.com/riot/account/v1/accounts/by-riot-id/"
        f"{quote(name)}/{quote(tag)}"
    )

async def get_summoner_by_puuid(puuid, region):
    platform, _, _ = REGIONS[region]
    return await riot_get(
        f"https://{platform}.api.riotgames.com/lol/summoner/v4/summoners/by-puuid/{puuid}"
    )

async def get_ranks(puuid, region):
    platform, _, _ = REGIONS[region]
    data = await riot_get(
        f"https://{platform}.api.riotgames.com/lol/league/v4/entries/by-puuid/{puuid}"
    )

    solo = flex = "UNRANKED"
    if data:
        for q in data:
            if q["queueType"] == "RANKED_SOLO_5x5":
                solo = q["tier"]
            elif q["queueType"] == "RANKED_FLEX_SR":
                flex = q["tier"]
    return solo, flex

# ------------------ ROLES (IDEMPOTENTES) ------------------

def get_desired_roles(guild, region, solo, flex):
    roles = []

    region_role = guild.get_role(REGIONS[region][2])
    solo_role = guild.get_role(SOLO_ROLES[solo])
    flex_role = guild.get_role(FLEX_ROLES[flex])

    for r in (region_role, solo_role, flex_role):
        if r:
            roles.append(r)

    return set(roles)

async def apply_roles(member, region, solo, flex):
    desired = get_desired_roles(member.guild, region, solo, flex)

    managed_ids = (
        list(SOLO_ROLES.values()) +
        list(FLEX_ROLES.values()) +
        [r[2] for r in REGIONS.values()]
    )

    current = {r for r in member.roles if r.id in managed_ids}

    to_add = desired - current
    to_remove = current - desired

    if not to_add and not to_remove:
        print(f"[ROLES] {member} sin cambios")
        return

    print(
        f"[ROLES] {member} "
        f"+{[r.name for r in to_add]} "
        f"-{[r.name for r in to_remove]}"
    )

    if to_remove:
        await member.remove_roles(*to_remove)
    if to_add:
        await member.add_roles(*to_add)

async def clear_roles(member):
    managed_ids = (
        list(SOLO_ROLES.values()) +
        list(FLEX_ROLES.values()) +
        [r[2] for r in REGIONS.values()]
    )

    roles = [r for r in member.roles if r.id in managed_ids]
    if roles:
        await member.remove_roles(*roles)

# ------------------ EMBEDS ------------------

def verification_embed(name, tag):
    embed = discord.Embed(
        title="🔐 Verificación de propiedad",
        description=(
            f"Para verificar que eres el dueño de **{name}#{tag}**:\n\n"
            "1️⃣ Abre League of Legends\n"
            "2️⃣ Cambia tu icono por el siguiente\n\n"
            "Pulsa **He cambiado el icono**"
        ),
        color=0xF1C40F
    )

    embed.set_thumbnail(
        url=f"https://raw.communitydragon.org/latest/plugins/"
            f"rcp-be-lol-game-data/global/default/v1/profile-icons/{VERIFICATION_ICON_ID}.jpg"
    )
    return embed

def build_account_embed(acc, summoner):
    embed = discord.Embed(
        title=f"{'⭐ ' if acc['primary'] else ''}{acc['riot_id']} ({acc['region']})",
        color=0x2B2D31
    )

    embed.set_thumbnail(
        url=f"https://raw.communitydragon.org/latest/plugins/"
            f"rcp-be-lol-game-data/global/default/v1/profile-icons/"
            f"{summoner['profileIconId']}.jpg"
    )

    embed.add_field(
        name="",
        value=f"Lvl {summoner['summonerLevel']}",
        inline=False
    )
    embed.add_field(
        name="",
        value=f"SoloQ: **{acc['solo']}**    FlexQ: **{acc['flex']}**",
        inline=False
    )
    return embed

# ------------------ VIEWS ------------------

class VerifyIconView(View):
    def __init__(self, user_id):
        super().__init__(timeout=300)
        self.user_id = user_id

    @discord.ui.button(
        label="He cambiado el icono",
        style=discord.ButtonStyle.success,
        custom_id="verify_icon"
    )
    async def verify(self, interaction, _):
        if str(interaction.user.id) != self.user_id:
            return await interaction.response.send_message(
                "❌ Esta verificación no es tuya.", ephemeral=True
            )

        pending = PENDING_VERIFICATIONS.get(self.user_id)
        if not pending:
            return await interaction.response.send_message(
                "⏰ Verificación expirada.", ephemeral=True
            )

        summoner = await get_summoner_by_puuid(
            pending["puuid"], pending["region"]
        )

        if summoner["profileIconId"] != VERIFICATION_ICON_ID:
            return await interaction.response.send_message(
                "❌ El icono no coincide.", ephemeral=True
            )

        solo, flex = await get_ranks(
            pending["puuid"], pending["region"]
        )

        data = load_data()
        data.setdefault(self.user_id, [])

        for a in data[self.user_id]:
            a["primary"] = False

        acc = {
            "riot_id": pending["riot_id"],
            "puuid": pending["puuid"],
            "region": pending["region"],
            "solo": solo,
            "flex": flex,
            "primary": True
        }

        data[self.user_id].append(acc)
        save_data(data)

        await apply_roles(interaction.user, acc["region"], solo, flex)
        del PENDING_VERIFICATIONS[self.user_id]

        await interaction.response.send_message(
            "✅ Cuenta vinculada correctamente",
            embed=build_account_embed(acc, summoner),
            ephemeral=True
        )

# ------------------ PANEL ------------------

class Panel(View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Actualizar datos",
        style=discord.ButtonStyle.success,
        custom_id="panel_refresh"
    )
    async def refresh(self, interaction, _):
        data = load_data()
        uid = str(interaction.user.id)

        if uid not in data:
            return await interaction.response.send_message(
                "No tienes cuenta principal.", ephemeral=True
            )

        primary = next(a for a in data[uid] if a["primary"])
        solo, flex = await get_ranks(
            primary["puuid"], primary["region"]
        )

        if solo == primary["solo"] and flex == primary["flex"]:
            return await interaction.response.send_message(
                "ℹ️ No hubo cambios de rango.", ephemeral=True
            )

        primary["solo"] = solo
        primary["flex"] = flex
        save_data(data)

        await apply_roles(
            interaction.user,
            primary["region"],
            solo,
            flex
        )

        await interaction.response.send_message(
            "🔄 Datos actualizados correctamente.",
            ephemeral=True
        )

# ------------------ TASK ------------------

@tasks.loop(hours=12)
async def update_ranks_loop():
    data = load_data()

    for uid, accounts in data.items():
        primary = next((a for a in accounts if a["primary"]), None)
        if not primary:
            continue

        solo, flex = await get_ranks(
            primary["puuid"], primary["region"]
        )

        if solo == primary["solo"] and flex == primary["flex"]:
            print(f"[RANKS] {uid} sin cambios")
            continue

        print(
            f"[RANKS] {uid} "
            f"{primary['solo']}/{primary['flex']} → {solo}/{flex}"
        )

        primary["solo"] = solo
        primary["flex"] = flex
        save_data(data)

        for guild in bot.guilds:
            member = guild.get_member(int(uid))
            if member:
                await apply_roles(
                    member,
                    primary["region"],
                    solo,
                    flex
                )

        await asyncio.sleep(0.5)

# ------------------ READY ------------------

@bot.event
async def on_ready():
    init_db()
    bot.add_view(Panel())
    update_ranks_loop.start()
    print("🤖 Bot listo")

# ------------------ WEB ------------------

app = Flask(__name__)

@app.route("/")
def home():
    return "Bot activo", 200

threading.Thread(
    target=lambda: app.run(host="0.0.0.0", port=3000),
    daemon=True
).start()

# ------------------ START ------------------

bot.run(TOKEN)
