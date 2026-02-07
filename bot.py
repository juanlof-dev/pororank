import discord
import aiohttp
import os
import threading
import asyncio  # <-- asegurarnos de que esté importado
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
VERIFICATION_ICON_ID = 29  # icono que debe ponerse el usuario

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

# ------------------ ROLES ------------------

async def clear_roles(member):
    ids = list(SOLO_ROLES.values()) + list(FLEX_ROLES.values()) + [r[2] for r in REGIONS.values()]
    for rid in ids:
        role = member.guild.get_role(rid)
        if role and role in member.roles:
            await member.remove_roles(role)

async def apply_roles(member, region, solo, flex):
    await clear_roles(member)
    await member.add_roles(
        member.guild.get_role(REGIONS[region][2]),
        member.guild.get_role(SOLO_ROLES[solo]),
        member.guild.get_role(FLEX_ROLES[flex])
    )

# ------------------ EMBEDS ------------------

def verification_embed(name, tag):
    embed = discord.Embed(
        title="🔐 Verificación de propiedad",
        description=(
            f"Para verificar que eres el dueño de **{name}#{tag}**:\n\n"
            "1️⃣ Abre el cliente de **League of Legends**\n"
            "2️⃣ Cambia tu **icono de invocador** por el siguiente\n\n"
            "Cuando lo hayas hecho, pulsa **He cambiado el icono**"
        ),
        color=0xF1C40F
    )
    embed.set_thumbnail(
        url=f"https://raw.communitydragon.org/latest/plugins/"
            f"rcp-be-lol-game-data/global/default/v1/profile-icons/{VERIFICATION_ICON_ID}.jpg"
    )
    return embed

def build_account_embed(acc, summoner):
    icon_url = (
        "https://raw.communitydragon.org/latest/plugins/"
        "rcp-be-lol-game-data/global/default/v1/profile-icons/"
        f"{summoner['profileIconId']}.jpg"
    )

    title = f"{'⭐ ' if acc['primary'] else ''}{acc['riot_id']} ({acc['region']})"

    embed = discord.Embed(
        title=title,
        color=0x2B2D31
    )
    embed.set_thumbnail(url=icon_url)

    embed.add_field(
        name="",
        value=f"**Lvl {summoner['summonerLevel']}**",
        inline=False
    )
    embed.add_field(
        name="",
        value=f"SoloQ: **{acc['solo']}**    FlexQ: **{acc['flex']}**",
        inline=False
    )

    embed.set_footer(text="Solo tú puedes verlo • Eliminar este mensaje")
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
            await interaction.response.send_message("❌ Esta verificación no es tuya.", ephemeral=True)
            return

        pending = PENDING_VERIFICATIONS.get(self.user_id)
        if not pending:
            await interaction.response.send_message("⏰ Verificación expirada.", ephemeral=True)
            return

        summoner = await get_summoner_by_puuid(pending["puuid"], pending["region"])
        if summoner["profileIconId"] != VERIFICATION_ICON_ID:
            await interaction.response.send_message(
                "❌ El icono no coincide. Cámbialo y vuelve a intentarlo.",
                ephemeral=True
            )
            return

        solo, flex = await get_ranks(pending["puuid"], pending["region"])
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

        embed = build_account_embed(acc, summoner)
        del PENDING_VERIFICATIONS[self.user_id]

        await interaction.response.send_message(
            "✅ **Cuenta vinculada correctamente**",
            embed=embed,
            ephemeral=True
        )

class AccountActionsView(View):
    def __init__(self, owner_id, index, is_primary: bool):
        super().__init__(timeout=None)
        self.owner_id = owner_id
        self.index = index

        # Deshabilitar botón si ya es principal
        if is_primary:
            self.primary.disabled = True
            self.primary.label = "Cuenta principal"
            self.primary.style = discord.ButtonStyle.secondary

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if str(interaction.user.id) != self.owner_id:
            await interaction.response.send_message(
                "❌ No puedes usar estos botones.",
                ephemeral=True
            )
            return False
        return True

    @discord.ui.button(
        label="Marcar principal",
        style=discord.ButtonStyle.success,
        custom_id="account_primary"
    )
    async def primary(self, interaction, _):
        data = load_data()
        accs = data[self.owner_id]

        for a in accs:
            a["primary"] = False
        accs[self.index]["primary"] = True
        save_data(data)

        acc = accs[self.index]

        await apply_roles(
            interaction.user,
            acc["region"],
            acc["solo"],
            acc["flex"]
        )

        summoner = await get_summoner_by_puuid(acc["puuid"], acc["region"])
        embed = build_account_embed(acc, summoner)

        await interaction.response.send_message(
            f"✅ Has marcado **{acc['riot_id']}** como tu cuenta principal",
            embed=embed,
            ephemeral=True
        )

    @discord.ui.button(
        label="Eliminar",
        style=discord.ButtonStyle.danger,
        custom_id="account_delete"
    )
    async def delete(self, interaction, _):
        data = load_data()
        accs = data[self.owner_id]

        removed = accs.pop(self.index)

        if accs:
            accs[0]["primary"] = True
            await apply_roles(
                interaction.user,
                accs[0]["region"],
                accs[0]["solo"],
                accs[0]["flex"]
            )
        else:
            await clear_roles(interaction.user)

        save_data(data)

        await interaction.response.send_message(
            f"🗑️ Cuenta **{removed['riot_id']}** eliminada.",
            ephemeral=True
        )

# ------------------ LINK FLOW ------------------

class RegionDropdown(Select):
    def __init__(self, name, tag):
        self.name = name
        self.tag = tag
        options = [discord.SelectOption(label=r, value=r) for r in REGIONS.keys()]
        super().__init__(placeholder="Selecciona región", options=options)

    async def callback(self, interaction):
        region = self.values[0]
        acc = await validate_riot_id(self.name, self.tag, region)

        if not acc:
            await interaction.response.send_message("❌ Riot ID no válido.", ephemeral=True)
            return

        PENDING_VERIFICATIONS[str(interaction.user.id)] = {
            "riot_id": f"{self.name}#{self.tag}",
            "puuid": acc["puuid"],
            "region": region
        }

        await interaction.response.send_message(
            embed=verification_embed(self.name, self.tag),
            view=VerifyIconView(str(interaction.user.id)),
            ephemeral=True
        )

class RegionView(View):
    def __init__(self, name, tag):
        super().__init__()
        self.add_item(RegionDropdown(name, tag))

class LinkModal(Modal, title="Vincular cuenta LoL"):
    riot = TextInput(label="Riot ID (Nombre#TAG)")

    async def on_submit(self, interaction):
        try:
            name, tag = self.riot.value.strip().split("#")
        except ValueError:
            await interaction.response.send_message("❌ Formato incorrecto.", ephemeral=True)
            return

        await interaction.response.send_message(
            "Selecciona la región:",
            view=RegionView(name, tag),
            ephemeral=True
        )

# ------------------ PANEL ------------------

class Panel(View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Vincular cuenta",
        style=discord.ButtonStyle.primary,
        custom_id="panel_link"
    )
    async def link(self, interaction, _):
        await interaction.response.send_modal(LinkModal())

    @discord.ui.button(
        label="Ver cuentas",
        style=discord.ButtonStyle.secondary,
        custom_id="panel_view_accounts"
    )
    async def view_accounts(self, interaction, _):
        data = load_data().get(str(interaction.user.id), [])
        if not data:
            await interaction.response.send_message(
                "No tienes cuentas vinculadas.",
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        for idx, acc in enumerate(data):
            summoner = await get_summoner_by_puuid(acc["puuid"], acc["region"])
            embed = build_account_embed(acc, summoner)

            view = AccountActionsView(
                owner_id=str(interaction.user.id),
                index=idx,
                is_primary=acc["primary"]
            )

            await interaction.followup.send(
                embed=embed,
                view=view,
                ephemeral=True
            )

    @discord.ui.button(
        label="Actualizar datos",
        style=discord.ButtonStyle.success,
        custom_id="panel_refresh"
    )
    async def refresh(self, interaction, _):
        data = load_data()
        uid = str(interaction.user.id)

        if uid not in data:
            await interaction.response.send_message(
                "No tienes cuenta principal.",
                ephemeral=True
            )
            return

        primary = next(a for a in data[uid] if a["primary"])
        solo, flex = await get_ranks(primary["puuid"], primary["region"])

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

# ------------------ DEPLOY PANEL ------------------

async def deploy_panel():
    channel = bot.get_channel(PANEL_CHANNEL_ID)
    if not channel:
        return

    await channel.purge(limit=5)

    embed = discord.Embed(
        title="🎮 Vinculación de Cuentas LoL",
        description=(
            "Gestiona tus cuentas de **League of Legends**, roles y rangos directamente desde este panel.\n\n"
            "🔹 **Vincular cuenta:** Añade tu cuenta de LoL\n"
            "🔹 **Ver cuentas:** Consulta tus cuentas vinculadas\n"
            "🔹 **Actualizar datos:** Refresca tu rango automáticamente"
        ),
        color=0x9146FF
    )

    embed.set_thumbnail(url="https://upload.wikimedia.org/wikipedia/en/7/77/League_of_Legends_Logo.png")
    embed.set_footer(text="Panel oficial de vinculación | ¡Mantén tus roles actualizados!", icon_url=bot.user.display_avatar.url)

    await channel.send(embed=embed, view=Panel())


# ------------------ TAREA AUTOMÁTICA DE RANKS ------------------

@tasks.loop(hours=3)
async def update_ranks_loop():
    print("🔄 Actualizando ranks de todos los usuarios...")
    data = load_data()
    for uid, accounts in data.items():
        # Buscar la cuenta principal
        primary_acc = next((a for a in accounts if a["primary"]), None)
        if not primary_acc:
            continue

        solo, flex = await get_ranks(primary_acc["puuid"], primary_acc["region"])
        primary_acc["solo"] = solo
        primary_acc["flex"] = flex
        save_data(data)

        # Aplicar roles si el usuario está en algún servidor donde el bot pueda
        for guild in bot.guilds:
            member = guild.get_member(int(uid))
            if member:
                await apply_roles(member, primary_acc["region"], solo, flex)

        # <-- Pausa para no saturar la API
        await asyncio.sleep(0.5)

    print("✅ Ranks actualizados correctamente.")

# ------------------ READY ------------------

@bot.event
async def on_ready():
    init_db()
    bot.add_view(Panel())
    bot.add_view(AccountActionsView("0", 0, False))
    await deploy_panel()

    # <--- Inicia la tarea automática de actualización de ranks
    update_ranks_loop.start()

    print("Bot listo")

# ------------------ WEB SERVER ------------------

app = Flask(__name__)

@app.route("/")
def home():
    return "Bot activo", 200

def run_flask():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 3000)))

threading.Thread(target=run_flask).start()

# ------------------ START ------------------

bot.run(TOKEN)





