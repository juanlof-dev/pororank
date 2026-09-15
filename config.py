import os
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("TOKEN")
RIOT_API_KEY = os.getenv("RIOT_API_KEY")

HYBRID_MODE = True  # 👈 IMPORTANTE

# 📌 Canales
PANEL_CHANNEL_ID = 1547312925809836033
LOG_CHANNEL_ID = 1410499822334640156

# 📌 Rol que tiene cualquiera que no ha vinculado ninguna cuenta. El bot lo
# quita al vincular con éxito, y lo vuelve a poner si se queda sin cuentas.
UNLINKED_ROLE_ID = 1547926475657969724

# 📌 Canal donde se registran los recordatorios de vinculación (24h/72h)
REMINDER_LOG_CHANNEL_ID = 1549497413675524287


REGIONS = {
    "EUW": ("euw1", "europe", 1547292715270807593),
    "EUNE": ("eun1", "europe", 1002),
    "NA": ("na1", "americas", 1003),
    "KR": ("kr", "asia", 1004),
    "BR": ("br1", "americas", 1005),
    "LAN": ("la1", "americas", 1409214864064249990),
    "LAS": ("la2", "americas", 1415758108428341379),
    "OCE": ("oc1", "americas", 1008),
    "JP": ("jp1", "asia", 1009),
    "TR": ("tr1", "europe", 1010),
    "RU": ("ru", "europe", 1011),
}

SOLO_ROLES = {
    "UNRANKED": 1547293381753831435,
    "IRON": 1547293370643255418,
    "BRONZE": 1547293360488980502,
    "SILVER": 1547293349692571799,
    "GOLD": 1547293338645045310,
    "PLATINUM": 1547293325110022195,
    "EMERALD": 1547293313734807704,
    "DIAMOND": 1547293302687137902,
    "MASTER": 1547293227038543963,
    "GRANDMASTER": 1547293205265911969,
    "CHALLENGER": 1547293109224873995,
}

FLEX_ROLES = {
    "UNRANKED": 1547297479496704010,
    "IRON": 1547297468943704154,
    "BRONZE": 1547297456658718882,
    "SILVER": 1547297448261591090,
    "GOLD": 1547297439357083838,
    "PLATINUM": 1547297430582726716,
    "EMERALD": 1547297421376229446,
    "DIAMOND": 1547297412681568376,
    "MASTER": 1547297403403505735,
    "GRANDMASTER": 1547297393362337810,
    "CHALLENGER": 1547297309065347102,
}

# 📌 Canales de "Buscar partida" — uno por tier de SoloQ
TIER_CHANNELS = {
    "UNRANKED": 1547299799550656592,     # #sin-clasificar
    "IRON": 1547299871361470484,          # #hierro
    "BRONZE": 1547299934678679644,        # #bronce
    "SILVER": 1547299999883202662,        # #plata
    "GOLD": 1547300037443326062,          # #oro
    "PLATINUM": 1547300099812499516,      # #platino
    "EMERALD": 1547300192594821131,       # #esmeralda
    "DIAMOND": 1547300248265957427,       # #diamante
    "MASTER": 1547300300862390382,        # #maestro
    "GRANDMASTER": 1547300350975811614,   # #granmaestro
    "CHALLENGER": 1547300414792147006,    # #aspirante
}

# 📌 A qué canales de tier se publica el aviso según el tier de SoloQ del
# jugador. Es una tabla explícita (no una fórmula) porque los extremos son
# asimétricos a propósito: Sinrango no se propaga a Hierro, y Challenger no
# tiene tier superior.
TIER_SEARCH_WINDOWS = {
    "UNRANKED": ["UNRANKED"],
    "IRON": ["IRON", "BRONZE"],
    "BRONZE": ["IRON", "BRONZE", "SILVER"],
    "SILVER": ["BRONZE", "SILVER", "GOLD"],
    "GOLD": ["SILVER", "GOLD", "PLATINUM"],
    "PLATINUM": ["GOLD", "PLATINUM", "EMERALD"],
    "EMERALD": ["PLATINUM", "EMERALD", "DIAMOND"],
    "DIAMOND": ["EMERALD", "DIAMOND", "MASTER"],
    "MASTER": ["DIAMOND", "MASTER", "GRANDMASTER"],
    "GRANDMASTER": ["MASTER", "GRANDMASTER", "CHALLENGER"],
    "CHALLENGER": ["GRANDMASTER", "CHALLENGER"],
}

# 📌 Roles de posición/lane asignados por las preguntas de incorporación
# (onboarding) del propio Discord — el bot solo los LEE, no los asigna.
LANE_ROLES = {
    "TOP": 1548391864808251628,      # Toplane
    "JUNGLE": 1548392101266456646,   # Jungla
    "MID": 1548392220321906770,      # Midlane
    "ADC": 1548392326177619979,      # Botlane
    "SUPPORT": 1548392411535908926,  # Soporte
}

# 📌 Cooldown del botón "Buscar partida", en segundos
SEARCH_COOLDOWN_SECONDS = 600  # 10 minutos

# 📌 Traducción de los tiers (tal como los devuelve la API de Riot) a lo que
# ve el usuario. Solo afecta a la presentación: toda la lógica interna sigue
# usando las claves de Riot (IRON, GOLD, etc.) sin tocar nada.
TIER_DISPLAY_ES = {
    "UNRANKED": "Sin rango",
    "IRON": "Hierro",
    "BRONZE": "Bronce",
    "SILVER": "Plata",
    "GOLD": "Oro",
    "PLATINUM": "Platino",
    "EMERALD": "Esmeralda",
    "DIAMOND": "Diamante",
    "MASTER": "Maestro",
    "GRANDMASTER": "Gran Maestro",
    "CHALLENGER": "Aspirante",
}

# 📌 Emojis personalizados por tier. Sin rango no lleva emoji (cadena vacía).
TIER_EMOJIS = {
    "UNRANKED": "",
    "IRON": "<:Hierro:1548994412540072047>",
    "BRONZE": "<:Bronce:1548994377261654118>",
    "SILVER": "<:Plata:1548994348014641313>",
    "GOLD": "<:Oro:1548994313944436817>",
    "PLATINUM": "<:Platino:1548994274140626974>",
    "EMERALD": "<:Esmeralda:1548994229307707463>",
    "DIAMOND": "<:Diamante:1548994195707011072>",
    "MASTER": "<:Maestro:1548994154992771092>",
    "GRANDMASTER": "<:Gran_Maestro:1548994124529537134>",
    "CHALLENGER": "<:Aspirante:1548993457694179388>",
}

# 📌 Emojis personalizados por lane, misma clave que LANE_ROLES.
LANE_EMOJIS = {
    "TOP": "<:toplane:1548290209647435856>",
    "JUNGLE": "<:jungla:1548290060258906132>",
    "MID": "<:midlane:1548290125488717864>",
    "ADC": "<:adc:1548290010514460763>",
    "SUPPORT": "<:support:1548290176093134990>",
}
