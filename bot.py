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
intents.message_content = True  # necesario si en el futuro usas comandos con prefijo "!"
bot = commands.Bot(command_prefix="!", intents=intents)

# ------------------ VERIFICACIÓN ------------------

PENDING_VERIFICATIONS = {}
VERIFICATION_ICON_ID = 25  # icono que debe ponerse el usuario

# ------------------ RIOT API ------------------

http_session: aiohttp.ClientSession | None = None

async def riot_get(url):
    headers = {"X-Riot-Token": RIOT_API_KEY}
    async with http_session.get(url, headers=headers) as r:
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

def get_desired_roles(member, region, solo, flex):
    roles = []
    region_role = member.guild.get_role(REGIONS[region][2])
    solo_role = member.guild.get_role(SOLO_ROLES[solo])
    flex_role = member.guild.get_role(FLEX_ROLES[flex])
    for r in (region_role, solo_role, flex_role):
        if r:
            roles.append(r)
    return set(roles)

async def apply_roles(member, region, solo, flex):
    desired = get_desired_roles(member, region, solo, flex)
    managed_ids = list(SOLO_ROLES.values()) + list(FLEX_ROLES.values()) + [r[2] for r in REGIONS.values()]
    current = {r for r in member.roles if r.id in managed_ids}

    to_add = desired - current
    to_remove = current - desired

    if not to_add and not to_remove:
        print(f"[ROLES] {member} sin cambios, no se tocarán roles")
        return

    print(f"[ROLES] {member} +{[r.name for r in to_add]} -{[r.name for r in to_remove]}")

    if to_remove:
        await member.remove_roles(*to_remove)
    if to_add:
        await member.add_roles(*to_add)

async def clear_roles(member):
    managed_ids = list(SOLO_ROLES.values()) + list(FLEX_ROLES.values()) + [r[2] for r in REGIONS.values()]
    roles = [r for r in member.roles if r.id in managed_ids]
    if roles:
        await member.remove_roles(*roles)

# ------------------ EMBEDS ------------------

def verification_embed(name, tag):
    embed = discord.Embed(
        title="🔐 Verificación de propiedad",
        description=(f"Para verificar que eres el dueño de **{name}#{tag}**:\n\n"
                     "1️⃣ Abre el cliente de **League of Legends**\n"
                     "2️⃣ Cambia tu **icono de invocador** por el siguiente\n\n"
                     "Cuando lo hayas hecho, pulsa **He cambiado el icono**"),
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
    embed = discord.Embed(title=title, color=0x2B2D31)
    embed.set_thumbnail(url=icon_url)
    embed.add_field(name="", value=f"**Lvl {summoner['summonerLevel']}**", inline=False)
    embed.add_field(name="", value=f"SoloQ: **{acc['solo']}**    FlexQ: **{acc['flex']}**", inline=False)
    embed.set_footer(text="Solo tú puedes verlo • Eliminar este mensaje")
    return embed

# ------------------ VIEWS ------------------

class VerifyIconView(View):
    def __init__(self, user_id):
        super().__init__(timeout=300)
        self.user_id = user_id

    @discord.ui.button(label="He cambiado el icono", style=discord.ButtonStyle.success, custom_id="verify_icon")
    async def verify(self, interaction, _):
        await interaction.response.defer(ephemeral=True)
        if str(interaction.user.id) != self.user_id:
            return await interaction.followup.send("❌ Esta verificación no es tuya.", ephemeral=True)

        pending = PENDING_VERIFICATIONS.get(self.user_id)
        if not pending:
            return await interaction.followup.send("⏰ Verificación expirada.", ephemeral=True)

        summoner = await get_summoner_by_puuid(pending["puuid"], pending["region"])
        if not summoner:
            return await interaction.followup.send("❌ No se pudieron obtener datos de Riot.", ephemeral=True)

        profile_icon = int(summoner.get("profileIconId", 0))
        if profile_icon != VERIFICATION_ICON_ID:
            return await interaction.followup.send(
                f"❌ El icono no coincide. Debe ser **{VERIFICATION_ICON_ID}**, "
                f"pero tu cuenta tiene **{profile_icon}**.",
                ephemeral=True
            )

        solo, flex = await get_ranks(pending["puuid"], pending["region"])
        data = load_data()
        data.setdefault(self.user_id, [])
        for a in data[self.user_id]:
            a["primary"] = False

        acc = {"riot_id": pending["riot_id"], "puuid": pending["puuid"],
               "region": pending["region"], "solo": solo, "flex": flex, "primary": True}
        data[self.user_id].append(acc)
        save_data(data)

        await apply_roles(interaction.user, acc["region"], solo, flex)
        del PENDING_VERIFICATIONS[self.user_id]

        await interaction.followup.send("✅ **Cuenta vinculada correctamente**",
                                        embed=build_account_embed(acc, summoner), ephemeral=True)

class AccountActionsView(View):
    def __init__(self, owner_id, puuid, is_primary: bool):
        # timeout finito: esta vista es efímera (nace y muere con el mensaje
        # "Ver cuentas"), no la volvemos a registrar como persistente tras un
        # reinicio para evitar que quede enganchada a datos de otra instancia.
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.puuid = puuid
        if is_primary:
            self.primary.disabled = True
            self.primary.label = "Cuenta principal"
            self.primary.style = discord.ButtonStyle.secondary

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if str(interaction.user.id) != self.owner_id:
            await interaction.response.send_message("❌ No puedes usar estos botones.", ephemeral=True)
            return False
        return True

    def _find_account(self, accs):
        """Localiza la cuenta por puuid (no por posición), para no
        desincronizarse si otra cuenta fue eliminada mientras tanto."""
        return next((i for i, a in enumerate(accs) if a["puuid"] == self.puuid), None)

    @discord.ui.button(label="Marcar principal", style=discord.ButtonStyle.success, custom_id="account_primary")
    async def primary(self, interaction, _):
        await interaction.response.defer(ephemeral=True)
        data = load_data()
        accs = data.get(self.owner_id, [])
        idx = self._find_account(accs)
        if idx is None:
            return await interaction.followup.send(
                "❌ Esta cuenta ya no existe (puede que la hayas eliminado desde otro mensaje).",
                ephemeral=True
            )

        for a in accs:
            a["primary"] = False
        accs[idx]["primary"] = True
        save_data(data)
        acc = accs[idx]
        await apply_roles(interaction.user, acc["region"], acc["solo"], acc["flex"])

        summoner = await get_summoner_by_puuid(acc["puuid"], acc["region"])
        if not summoner:
            return await interaction.followup.send(
                f"✅ Has marcado **{acc['riot_id']}** como principal, pero no se pudieron "
                "obtener sus datos actuales de Riot (inténtalo de nuevo más tarde).",
                ephemeral=True
            )
        embed = build_account_embed(acc, summoner)
        await interaction.followup.send(f"✅ Has marcado **{acc['riot_id']}** como tu cuenta principal",
                                        embed=embed, ephemeral=True)

    @discord.ui.button(label="Eliminar", style=discord.ButtonStyle.danger, custom_id="account_delete")
    async def delete(self, interaction, _):
        await interaction.response.defer(ephemeral=True)
        data = load_data()
        accs = data.get(self.owner_id, [])
        idx = self._find_account(accs)
        if idx is None:
            return await interaction.followup.send(
                "❌ Esta cuenta ya no existe (puede que ya la hubieras eliminado).",
                ephemeral=True
            )

        removed = accs.pop(idx)
        if accs:
            accs[0]["primary"] = True
            await apply_roles(interaction.user, accs[0]["region"], accs[0]["solo"], accs[0]["flex"])
        else:
            await clear_roles(interaction.user)
        save_data(data)
        await interaction.followup.send(f"🗑️ Cuenta **{removed['riot_id']}** eliminada.", ephemeral=True)

# ------------------ LINK FLOW ------------------

class RegionDropdown(Select):
    def __init__(self, name, tag):
        self.name = name
        self.tag = tag
        options = [discord.SelectOption(label=r, value=r) for r in REGIONS.keys()]
        super().__init__(placeholder="Selecciona región", options=options)

    async def callback(self, interaction):
        await interaction.response.defer(ephemeral=True)
        region = self.values[0]
        acc = await validate_riot_id(self.name, self.tag, region)
        if not acc:
            return await interaction.followup.send("❌ Riot ID no válido.", ephemeral=True)

        PENDING_VERIFICATIONS[str(interaction.user.id)] = {"riot_id": f"{self.name}#{self.tag}", "puuid": acc["puuid"], "region": region}
        await interaction.followup.send(embed=verification_embed(self.name, self.tag),
                                        view=VerifyIconView(str(interaction.user.id)), ephemeral=True)

class RegionView(View):
    def __init__(self, name, tag):
        super().__init__()
        self.add_item(RegionDropdown(name, tag))

class LinkModal(Modal):
    def __init__(self):
        super().__init__(title="Vincular cuenta LoL")

        self.name = TextInput(
            label="Nombre de invocador",
            placeholder="Ej: XOKAS THE KING",
            max_length=16
        )

        self.tag = TextInput(
            label="TAG",
            placeholder="KEKY",
            max_length=5
        )

        self.add_item(self.name)
        self.add_item(self.tag)

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True)

        name = self.name.value.strip()
        tag = self.tag.value.strip().upper()

        if "#" in name or "#" in tag:
            return await interaction.followup.send(
                "❌ No incluyas el carácter **#**.\n"
                "👉 Escríbelo separado: **Nombre** y **TAG**.",
                ephemeral=True
            )

        if not name or not tag:
            return await interaction.followup.send(
                "❌ Debes rellenar ambos campos.",
                ephemeral=True
            )

        await interaction.followup.send(
            "Selecciona la región:",
            view=RegionView(name, tag),
            ephemeral=True
        )

# ------------------ PANEL ------------------

class Panel(View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Vincular cuenta", style=discord.ButtonStyle.primary, custom_id="panel_link")
    async def link(self, interaction, _):
        await interaction.response.send_modal(LinkModal())

    @discord.ui.button(label="Ver cuentas", style=discord.ButtonStyle.secondary, custom_id="panel_view_accounts")
    async def view_accounts(self, interaction, _):
        await interaction.response.defer(ephemeral=True)
        data = load_data().get(str(interaction.user.id), [])
        if not data:
            return await interaction.followup.send("No tienes cuentas vinculadas.", ephemeral=True)
        for acc in data:
            summoner = await get_summoner_by_puuid(acc["puuid"], acc["region"])
            if not summoner:
                await interaction.followup.send(
                    f"⚠️ No se pudieron obtener los datos de **{acc['riot_id']}** ahora mismo "
                    "(Riot API no respondió). Inténtalo de nuevo en unos minutos.",
                    ephemeral=True
                )
                continue
            embed = build_account_embed(acc, summoner)
            view = AccountActionsView(owner_id=str(interaction.user.id), puuid=acc["puuid"], is_primary=acc["primary"])
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(label="Actualizar datos", style=discord.ButtonStyle.success, custom_id="panel_refresh")
    async def refresh(self, interaction, _):
        await interaction.response.defer(ephemeral=True)
        data = load_data()
        uid = str(interaction.user.id)
        if uid not in data:
            return await interaction.followup.send("No tienes cuenta principal.", ephemeral=True)

        primary = next(a for a in data[uid] if a["primary"])
        solo, flex = await get_ranks(primary["puuid"], primary["region"])

        if solo != primary["solo"] or flex != primary["flex"]:
            primary["solo"] = solo
            primary["flex"] = flex
            save_data(data)
            await apply_roles(interaction.user, primary["region"], solo, flex)

        await interaction.followup.send("🔄 Datos actualizados correctamente.", ephemeral=True)

# ------------------ REFRESCO AUTOMÁTICO DE RANGOS ------------------

@tasks.loop(hours=12)
async def update_ranks_loop():
    print("🔄 Actualizando ranks de todos los usuarios...")
    data = load_data()
    changed = False

    for uid, accounts in data.items():
        primary_acc = next((a for a in accounts if a["primary"]), None)
        if not primary_acc:
            continue

        solo, flex = await get_ranks(primary_acc["puuid"], primary_acc["region"])
        if solo == primary_acc["solo"] and flex == primary_acc["flex"]:
            print(f"[RANKS] {uid} sin cambios")
            continue

        print(f"[RANKS] {uid}: {primary_acc['solo']}/{primary_acc['flex']} → {solo}/{flex}")
        primary_acc["solo"] = solo
        primary_acc["flex"] = flex
        changed = True

        for guild in bot.guilds:
            member = guild.get_member(int(uid))
            if member:
                await apply_roles(member, primary_acc["region"], solo, flex)

        await asyncio.sleep(0.5)

    if changed:
        save_data(data)
    print("✅ Ranks actualizados correctamente.")

# ------------------ FUNCION DEPLOY PANEL ------------------

async def deploy_panel():
    channel = bot.get_channel(PANEL_CHANNEL_ID)
    if not channel:
        print(f"❌ No se encontró el canal con ID {PANEL_CHANNEL_ID}")
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
    embed.set_footer(text="Panel oficial de vinculación | ¡Mantén tus roles actualizados!",
                     icon_url=bot.user.display_avatar.url)
    await channel.send(embed=embed, view=Panel())

# ------------------ READY ------------------
@bot.event
async def on_ready():
    init_db()

    # ---------- VISTAS PERSISTENTES ----------
    # Solo el Panel necesita sobrevivir a reinicios: es un mensaje fijo en un
    # canal, sin estado por-usuario en sus custom_id. AccountActionsView es
    # efímera (mensajes "Ver cuentas" de vida corta) y lleva estado propio
    # (owner_id, puuid), así que NO se registra aquí para evitar que un
    # reinicio reenganche interacciones reales a una instancia con datos falsos.
    bot.add_view(Panel())

    await deploy_panel()
    update_ranks_loop.start()

    print("🤖 Bot listo")

# ------------------ WEB SERVER ------------------

app = Flask(__name__)

@app.route("/")
def home():
    return "Bot activo", 200

def run_flask():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 3000)))

# daemon=True: que este hilo no impida cerrar el proceso si el bot termina
threading.Thread(target=run_flask, daemon=True).start()

# ------------------ START ------------------

async def main():
    global http_session
    async with aiohttp.ClientSession() as session:
        http_session = session
        async with bot:
            await bot.start(TOKEN)

asyncio.run(main())
