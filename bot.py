# ================== IMPORTS ==================

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

# ================== BOT ==================

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ================== DUOQ CONFIG ==================

DUOQ_CHANNELS = {
    # CHANNEL_ID : "ELO"
    1466033982637604975: "UNRANKED",
    1466038367937363968: "IRON",
    1466038452939128872: "BRONZE",
    1466038758074748968: "SILVER",
    1466038917823201361: "GOLD",
    1466039070118383647: "PLATINUM",
    1466039277031784550: "EMERALD",
    1466039419671679083: "DIAMOND",
}

DUO_STATES = {}  # message_id -> state

# ================== RIOT API ==================

async def riot_get(url):
    headers = {"X-Riot-Token": RIOT_API_KEY}
    async with aiohttp.ClientSession() as s:
        async with s.get(url, headers=headers) as r:
            if r.status != 200:
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

# ================== HELPERS ==================

def get_elo_from_channel(channel_id):
    return DUOQ_CHANNELS.get(channel_id)

def user_has_elo_role(member, elo):
    role_id = SOLO_ROLES.get(elo)
    return role_id and any(r.id == role_id for r in member.roles)

def get_user_region(member):
    for region, (_, _, role_id) in REGIONS.items():
        if any(r.id == role_id for r in member.roles):
            return region
    return None

# ================== DUO EMBED ==================

def build_duo_embed(user, state):
    embed = discord.Embed(
        title=f"🔎 {user.display_name} busca DUO",
        description=f"**{state['elo']} · {state['region']}**",
        color=0x5865F2
    )

    embed.add_field(name="🧭 Posición", value=state["position"] or "❓", inline=True)
    embed.add_field(name="🔥 Actitud", value=state["attitude"] or "❓", inline=True)
    embed.add_field(
        name="🎧 Voz",
        value="✅" if state["voice"] else "❌" if state["voice"] is not None else "❓",
        inline=True
    )

    embed.add_field(name="🏆 Rank", value=state["elo"], inline=True)
    embed.add_field(name="🌍 Región", value=state["region"], inline=True)
    embed.add_field(name="⭐ Duo Rating", value="—", inline=True)

    embed.set_thumbnail(url=user.display_avatar.url)
    embed.set_footer(text="DuoQ System")

    return embed

# ================== DUO VIEW ==================

class DuoPositionSelect(Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Toplane", emoji="🛡️"),
            discord.SelectOption(label="Jungla", emoji="🌲"),
            discord.SelectOption(label="Midlane", emoji="⚡"),
            discord.SelectOption(label="ADC", emoji="🏹"),
            discord.SelectOption(label="Support", emoji="🩹"),
        ]
        super().__init__(placeholder="Selecciona posición", options=options)

    async def callback(self, interaction):
        state = DUO_STATES.get(interaction.message.id)
        if interaction.user.id != state["author_id"]:
            return await interaction.response.send_message(
                "❌ Solo el autor puede usar estos botones.",
                ephemeral=True
            )

        state["position"] = self.values[0]
        await interaction.response.edit_message(
            embed=build_duo_embed(interaction.user, state),
            view=self.view
        )

class DuoButton(discord.ui.Button):
    def __init__(self, label, style, action):
        super().__init__(label=label, style=style)
        self.action = action

    async def callback(self, interaction):
        state = DUO_STATES.get(interaction.message.id)
        if interaction.user.id != state["author_id"]:
            return await interaction.response.send_message(
                "❌ Solo el autor puede usar estos botones.",
                ephemeral=True
            )

        if self.action == "chill":
            state["attitude"] = "Chill"
        elif self.action == "tryhard":
            state["attitude"] = "Tryhard"
        elif self.action == "voice":
            state["voice"] = not state["voice"] if state["voice"] is not None else True
        elif self.action == "finalize":
            if None in (state["position"], state["attitude"], state["voice"]):
                return await interaction.response.send_message(
                    "❌ Completa todas las opciones antes de buscar duo.",
                    ephemeral=True
                )

            guild = interaction.guild
            region_role = guild.get_role(REGIONS[state["region"]][2])
            elo_role = guild.get_role(SOLO_ROLES[state["elo"]])

            await interaction.channel.send(
                f"{region_role.mention} {elo_role.mention} {interaction.user.mention}"
            )
            await interaction.response.edit_message(view=None)
            return

        await interaction.response.edit_message(
            embed=build_duo_embed(interaction.user, state),
            view=self.view
        )

class DuoView(View):
    def __init__(self):
        super().__init__(timeout=900)
        self.add_item(DuoPositionSelect())
        self.add_item(DuoButton("😌 Chill", discord.ButtonStyle.secondary, "chill"))
        self.add_item(DuoButton("🔥 Tryhard", discord.ButtonStyle.secondary, "tryhard"))
        self.add_item(DuoButton("🎧 Voz", discord.ButtonStyle.primary, "voice"))
        self.add_item(DuoButton("Buscar Duo", discord.ButtonStyle.success, "finalize"))

# ================== SLASH COMMAND ==================

@bot.tree.command(name="duo", description="Buscar compañero para DuoQ")
async def duo(interaction: discord.Interaction):

    elo = get_elo_from_channel(interaction.channel_id)
    if not elo:
        return await interaction.response.send_message(
            "❌ Este comando solo funciona en canales de DuoQ.",
            ephemeral=True
        )

    if not user_has_elo_role(interaction.user, elo):
        return await interaction.response.send_message(
            f"❌ Necesitas el rol **{elo}** para usar este canal.",
            ephemeral=True
        )

    region = get_user_region(interaction.user)
    if not region:
        return await interaction.response.send_message(
            "❌ No tienes región asignada.",
            ephemeral=True
        )

    state = {
        "author_id": interaction.user.id,
        "elo": elo,
        "region": region,
        "position": None,
        "attitude": None,
        "voice": None
    }

    embed = build_duo_embed(interaction.user, state)
    view = DuoView()

    await interaction.response.send_message(embed=embed, view=view)
    msg = await interaction.original_response()
    DUO_STATES[msg.id] = state

# ================== READY ==================

@bot.event
async def on_ready():
    init_db()
    await bot.tree.sync()
    print("Bot listo")

# ================== START ==================

bot.run(TOKEN)
