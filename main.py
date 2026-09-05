from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import secrets
import shlex
import threading
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Callable

import discord
from discord import app_commands
from discord.ext import commands
from flask import Flask, redirect, render_template_string, request, send_file, session, url_for
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps


# -----------------------------------------------------------------------------
# Persistent storage
# -----------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
DATA_PATH = BASE_DIR / "data.json"
GENERATED_DIR = BASE_DIR / "generated"
LOGO_PATH = BASE_DIR / "logo.png"
REFERENCE_BG_PATH = BASE_DIR / "attached_assets" / "4F5C47D4-ECD9-4C99-9370-98B7031A3959_1788617466670.png"
GENERATED_DIR.mkdir(exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("staff-points")
DATA_LOCK = threading.RLock()

DEFAULT_GUILD_DATA: dict[str, Any] = {
    "points": {},
    "members": {},
    "shortcuts": {},
    "rank_tiers": [],
    "settings": {
        "dashboard_title": "D70 Staff Operations",
        "server_name": "D70 STAFF",
        "staff_role_name": "",
        "owner_user_id": "",
        "accent_color": "#e51e2a",
    },
}

DEFAULT_DATA: dict[str, Any] = {
    "guilds": {},
    "bot_admins": {},
    "legacy_migrated_to": "",
    "points": {},
    "members": {},
    "shortcuts": {},
    "settings": {},
}


def load_data() -> dict[str, Any]:
    data = json.loads(json.dumps(DEFAULT_DATA))
    if not DATA_PATH.exists():
        return data
    try:
        source = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("data.json is invalid; starting with empty records.")
        return data
    if not isinstance(source, dict):
        return data
    for key in ("points", "members", "shortcuts"):
        if isinstance(source.get(key), dict):
            data[key].update(source[key])
    if isinstance(source.get("settings"), dict):
        data["settings"].update(source["settings"])
    if isinstance(source.get("guilds"), dict):
        data["guilds"].update(source["guilds"])
    if isinstance(source.get("bot_admins"), dict):
        data["bot_admins"].update(source["bot_admins"])
    if str(source.get("legacy_migrated_to", "")).isdigit():
        data["legacy_migrated_to"] = str(source["legacy_migrated_to"])
    return data


DATA = load_data()


def save_data() -> None:
    with DATA_LOCK:
        temporary = DATA_PATH.with_suffix(".tmp")
        temporary.write_text(json.dumps(DATA, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(DATA_PATH)


def guild_data(guild_id: int | str) -> dict[str, Any]:
    key = str(guild_id)
    guilds = DATA.setdefault("guilds", {})
    changed = False
    if key not in guilds:
        guilds[key] = json.loads(json.dumps(DEFAULT_GUILD_DATA))
        changed = True
    store = guilds[key]
    for section in ("points", "members", "shortcuts", "settings"):
        if not isinstance(store.get(section), dict):
            store[section] = {}
            changed = True
    if not isinstance(store.get("rank_tiers"), list):
        store["rank_tiers"] = []
        changed = True
    migration_target = GUILD_ID if globals().get("GUILD_ID", "").isdigit() else key
    if not DATA.get("legacy_migrated_to") and key == migration_target:
        for section in ("points", "members", "shortcuts"):
            if isinstance(DATA.get(section), dict):
                store[section].update(DATA[section])
                DATA[section] = {}
        if isinstance(DATA.get("settings"), dict):
            store["settings"].update(DATA["settings"])
            DATA["settings"] = {}
        DATA["legacy_migrated_to"] = key
        changed = True
    for name, value in DEFAULT_GUILD_DATA["settings"].items():
        if name not in store["settings"]:
            store["settings"][name] = value
            changed = True
    if changed:
        save_data()
    return store


def setting(name: str, default: str = "", guild_id: int | str | None = None) -> str:
    source = guild_data(guild_id)["settings"] if guild_id is not None else DATA.get("settings", {})
    value = source.get(name, default)
    return str(value) if value is not None else default


def points_for(user_id: int | str, guild_id: int | str) -> int:
    try:
        return max(0, int(guild_data(guild_id)["points"].get(str(user_id), 0)))
    except (TypeError, ValueError):
        return 0


def rank_tiers_for(guild_id: int | str) -> list[dict[str, Any]]:
    tiers: list[dict[str, Any]] = []
    with DATA_LOCK:
        raw_tiers = json.loads(json.dumps(guild_data(guild_id).get("rank_tiers", [])))
    for raw in raw_tiers:
        if not isinstance(raw, dict):
            continue
        role_id = str(raw.get("role_id", "")).strip()
        if not role_id.isdigit():
            continue
        try:
            points = max(0, int(raw.get("points", 0)))
        except (TypeError, ValueError):
            points = 0
        tiers.append({
            "role_id": role_id,
            "name": str(raw.get("name", "رتبة إدارية")).strip()[:80] or "رتبة إدارية",
            "category": "senior" if raw.get("category") == "senior" else "junior",
            "points": points,
        })
    return sorted(
        tiers,
        key=lambda tier: (
            tier["points"],
            1 if tier["category"] == "senior" else 0,
            tier["name"].casefold(),
        ),
    )


def set_points(
    user_id: int | str,
    amount: int,
    display_name: str,
    guild_id: int | str,
) -> int:
    with DATA_LOCK:
        store = guild_data(guild_id)
        member_id = str(user_id)
        store["points"][member_id] = max(0, int(amount))
        if display_name.strip():
            store["members"][member_id] = display_name.strip()[:80]
        save_data()
        return store["points"][member_id]


def change_points(
    user_id: int | str,
    delta: int,
    display_name: str,
    guild_id: int | str,
) -> int:
    with DATA_LOCK:
        return set_points(
            user_id,
            points_for(user_id, guild_id) + delta,
            display_name,
            guild_id,
        )


# -----------------------------------------------------------------------------
# Discord bot
# -----------------------------------------------------------------------------

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing. Add it as an environment secret.")

OWNER_USER_ID = os.getenv("OWNER_USER_ID", "").strip()
GUILD_ID = os.getenv("DISCORD_GUILD_ID", "").strip()

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)
BOT_LOOP: asyncio.AbstractEventLoop | None = None
REGISTERED_SHORTCUTS: set[str] = set()
BUILTIN_COMMANDS = {"addpoints", "removepoints", "shortcut", "me", "leaderboard"}
RANK_SYNC_LOCKS: dict[tuple[int, int], asyncio.Lock] = {}
DASHBOARD_PERMISSIONS = {"points", "shortcuts", "settings", "ranks"}
DASHBOARD_PERMISSION_LABELS = {
    "points": "إدارة النقاط",
    "shortcuts": "إدارة الاختصارات",
    "settings": "إعدادات السيرفر",
    "ranks": "الرتب والترقية التلقائية",
}


def bot_admin_permissions(user_id: int | str) -> set[str]:
    with DATA_LOCK:
        entry = json.loads(json.dumps(DATA.get("bot_admins", {}).get(str(user_id), {})))
    if not isinstance(entry, dict):
        return set()
    permissions = entry.get("permissions", [])
    if not isinstance(permissions, list):
        return set()
    return {str(permission) for permission in permissions} & DASHBOARD_PERMISSIONS


def is_bot_admin(user_id: int | str, permission: str | None = None) -> bool:
    entry = DATA.get("bot_admins", {}).get(str(user_id))
    if not isinstance(entry, dict):
        return False
    return permission is None or permission in bot_admin_permissions(user_id)


def extract_id(value: str) -> int | None:
    match = re.fullmatch(r"<@!?(\d+)>", value.strip())
    if match:
        return int(match.group(1))
    return int(value.strip()) if value.strip().isdigit() else None


def role_name(member: discord.Member) -> str:
    return member.top_role.name if member.top_role.name != "@everyone" else "Staff Member"


def is_manager(interaction: discord.Interaction, permission: str = "points") -> bool:
    configured_owner = OWNER_USER_ID or setting(
        "owner_user_id",
        guild_id=interaction.guild_id,
    )
    if configured_owner and str(interaction.user.id) == configured_owner:
        return True
    if is_bot_admin(interaction.user.id, permission):
        return True
    return isinstance(interaction.user, discord.Member) and (
        interaction.user.guild_permissions.manage_guild
        or interaction.user.guild_permissions.administrator
    )


async def require_manager(interaction: discord.Interaction, permission: str = "points") -> bool:
    if is_manager(interaction, permission):
        return True
    message = "هذا الأمر متاح للإدارة فقط (Manage Server / Administrator)."
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)
    return False


def valid_staff_role(member: discord.Member) -> bool:
    configured = setting("staff_role_name", guild_id=member.guild.id).strip().casefold()
    configured_rank_ids = {tier["role_id"] for tier in rank_tiers_for(member.guild.id)}
    has_configured_rank = any(str(role.id) in configured_rank_ids for role in member.roles)
    return (
        not configured
        or has_configured_rank
        or any(role.name.casefold() == configured for role in member.roles)
    )


def clean_command_name(value: str) -> str:
    name = value.strip().lower().lstrip("/")
    if not re.fullmatch(r"[a-z0-9_-]{1,32}", name):
        raise ValueError("اسم الاختصار يجب أن يكون إنجليزيًا من 1 إلى 32 حرفًا.")
    return name


def resolve_target(value: str, guild_id: int | str) -> str | None:
    shortcuts = guild_data(guild_id)["shortcuts"]
    current = value.strip().lower().lstrip("/")
    visited: set[str] = set()
    while current in shortcuts and current not in visited:
        visited.add(current)
        current = str(shortcuts[current]).lower().lstrip("/")
    return current if current in BUILTIN_COMMANDS else None


# -----------------------------------------------------------------------------
# Luxury image generation: every successful command returns an image only.
# -----------------------------------------------------------------------------

def font(
    size: int,
    bold: bool = False,
    display: bool = False,
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    if display:
        names = (
            ["/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf"]
            if bold
            else ["/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf"]
        )
    else:
        names = (
            ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
            if bold
            else ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
        )
    for name in names:
        if Path(name).exists():
            return ImageFont.truetype(name, size)
    return ImageFont.load_default()


def background(width: int, height: int) -> Image.Image:
    if REFERENCE_BG_PATH.exists():
        source = Image.open(REFERENCE_BG_PATH).convert("RGB")
        # Remove the reference mark from the texture; the centered mark is
        # composited separately by add_logo().
        source = source.crop((220, 0, source.width, source.height))
        canvas = ImageOps.fit(
            source,
            (width, height),
            method=Image.Resampling.LANCZOS,
            centering=(0.52, 0.5),
        ).convert("RGBA")
    else:
        canvas = Image.new("RGBA", (width, height), (9, 10, 11, 255))

    canvas = Image.alpha_composite(
        canvas,
        Image.new("RGBA", canvas.size, (0, 0, 0, 42)),
    )
    vignette = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    vignette_draw = ImageDraw.Draw(vignette)
    vignette_draw.rectangle(
        (0, 0, width - 1, height - 1),
        outline=(0, 0, 0, 215),
        width=max(80, width // 11),
    )
    canvas = Image.alpha_composite(
        canvas,
        vignette.filter(ImageFilter.GaussianBlur(max(45, width // 22))),
    )
    draw = ImageDraw.Draw(canvas)
    for index in range(58):
        x = (index * 223 + 41) % width
        y = (index * 137 + 67) % height
        shade = 34 + index % 14
        draw.point((x, y), fill=(shade, shade, shade, 65))
    draw.rounded_rectangle(
        (28, 28, width - 28, height - 28),
        radius=18,
        outline=(195, 198, 202, 38),
        width=1,
    )
    return canvas


def add_logo(canvas: Image.Image, draw: ImageDraw.ImageDraw) -> None:
    if REFERENCE_BG_PATH.exists():
        try:
            reference = Image.open(REFERENCE_BG_PATH).convert("RGB")
            mark = reference.crop((50, 55, 220, 135)).convert("L")
            alpha = mark.point(lambda value: max(0, min(255, (value - 42) * 5)))
            logo = Image.new("RGBA", mark.size, (214, 216, 218, 0))
            logo.putalpha(alpha)
            bounds = logo.getbbox()
            if bounds:
                logo = logo.crop(bounds)
            scale = min(185 / logo.width, 58 / logo.height)
            logo = logo.resize(
                (max(1, int(logo.width * scale)), max(1, int(logo.height * scale))),
                Image.Resampling.LANCZOS,
            )
            x = (canvas.width - logo.width) // 2
            y = 52
            canvas.alpha_composite(logo, (x, y))
            draw.line(
                (canvas.width // 2 - 110, y + logo.height + 26,
                 canvas.width // 2 + 110, y + logo.height + 26),
                fill=(192, 195, 200, 42),
                width=1,
            )
            return
        except (OSError, ValueError):
            logger.warning("Reference background could not be read; using fallback logo.")
    fallback = font(38, True, True)
    bounds = draw.textbbox((0, 0), "D70", font=fallback)
    x = (canvas.width - (bounds[2] - bounds[0])) // 2
    draw.text((x, 52), "D70", fill=(214, 216, 218, 255), font=fallback)


def draw_centered(
    draw: ImageDraw.ImageDraw,
    canvas_width: int,
    y: int,
    text: str,
    text_font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    fill: tuple[int, int, int, int],
) -> None:
    bounds = draw.textbbox((0, 0), text, font=text_font)
    x = (canvas_width - (bounds[2] - bounds[0])) // 2 - bounds[0]
    draw.text((x, y), text, fill=fill, font=text_font)


def draw_right(
    draw: ImageDraw.ImageDraw,
    right: int,
    y: int,
    text: str,
    text_font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    fill: tuple[int, int, int, int],
) -> None:
    bounds = draw.textbbox((0, 0), text, font=text_font)
    draw.text((right - (bounds[2] - bounds[0]), y), text, fill=fill, font=text_font)


def create_profile_image(member: discord.Member, rank: int, total: int) -> Path:
    canvas = background(1400, 900)
    draw = ImageDraw.Draw(canvas)
    add_logo(canvas, draw)
    white = (232, 233, 235, 255)
    silver = (164, 167, 172, 255)
    dim = (92, 95, 101, 255)
    draw_centered(draw, canvas.width, 158, "STAFF PROFILE", font(20, True), silver)
    draw_centered(draw, canvas.width, 210, member.display_name[:28], font(58, True, True), white)
    draw.line((360, 302, 1040, 302), fill=(177, 180, 185, 54), width=1)
    rows = [("RANK", f"#{rank} / {max(total, 1)}"),
            ("ROLE", role_name(member)[:28]),
            ("POINTS", f"{points_for(member.id, member.guild.id):,}")]
    centers = (300, 700, 1100)
    for center, (label, value) in zip(centers, rows):
        value_font = font(34 if label != "ROLE" else 29, True, True)
        bounds = draw.textbbox((0, 0), value, font=value_font)
        draw.text((center - (bounds[2] - bounds[0]) // 2, 410), value, fill=white, font=value_font)
        label_font = font(17, True)
        bounds = draw.textbbox((0, 0), label, font=label_font)
        draw.text((center - (bounds[2] - bounds[0]) // 2, 475), label, fill=silver, font=label_font)
    draw.line((250, 555, 1150, 555), fill=(177, 180, 185, 35), width=1)
    draw_centered(
        draw,
        canvas.width,
        715,
        f"D70 STAFF OPERATIONS  /  {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
        font(16, True),
        dim,
    )
    output = GENERATED_DIR / f"profile_{member.id}.png"
    canvas.convert("RGB").save(output, "PNG", optimize=True)
    return output


def leaderboard_rows(guild: discord.Guild) -> list[tuple[discord.Member | None, str, int]]:
    store = guild_data(guild.id)
    rows: list[tuple[discord.Member | None, str, int]] = []
    for member_id in store["points"]:
        points = points_for(member_id, guild.id)
        if points <= 0:
            continue
        member = guild.get_member(int(member_id)) if str(member_id).isdigit() else None
        name = member.display_name if member else store["members"].get(member_id, f"Member {member_id}")
        rows.append((member, name, points))
    return sorted(rows, key=lambda row: (-row[2], row[1].casefold()))


def create_leaderboard_image(guild: discord.Guild, rows: list[tuple[discord.Member | None, str, int]]) -> Path:
    canvas = background(1400, 900)
    draw = ImageDraw.Draw(canvas)
    add_logo(canvas, draw)
    white = (232, 233, 235, 255)
    silver = (157, 160, 165, 255)
    dim = (91, 94, 100, 255)
    draw_centered(
        draw,
        canvas.width,
        145,
        setting("server_name", guild.name, guild.id)[:34],
        font(47, True, True),
        white,
    )
    draw_centered(draw, canvas.width, 214, "STAFF LEADERBOARD", font(18, True), silver)
    draw.line((140, 270, 1260, 270), fill=(177, 180, 185, 48), width=1)
    if not rows:
        draw_centered(draw, canvas.width, 410, "NO POINTS RECORDED YET", font(29, True), silver)
    for index, (_, name, points) in enumerate(rows[:8], start=1):
        y = 296 + (index - 1) * 61
        if index % 2:
            draw.rounded_rectangle((138, y - 5, 1262, y + 49), radius=8, fill=(5, 6, 7, 74))
        draw.text((165, y + 4), f"{index:02d}", fill=white if index <= 3 else dim, font=font(23, True))
        draw.text((250, y + 2), name[:30], fill=white, font=font(25, True))
        draw_right(draw, 1235, y + 3, f"{points:,} PTS", font(23, True), silver)
    draw_centered(
        draw,
        canvas.width,
        820,
        f"{len(rows)} STAFF RECORDS  /  UPDATED {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        font(15, True),
        dim,
    )
    output = GENERATED_DIR / f"leaderboard_{guild.id}.png"
    canvas.convert("RGB").save(output, "PNG", optimize=True)
    return output


def create_points_action_image(
    member: discord.Member,
    amount: int,
    total: int,
    added: bool,
) -> Path:
    canvas = background(1400, 900)
    draw = ImageDraw.Draw(canvas)
    add_logo(canvas, draw)
    white = (232, 233, 235, 255)
    silver = (159, 162, 167, 255)
    dim = (91, 94, 100, 255)
    title = "POINTS AWARDED" if added else "POINTS REMOVED"
    sign = "+" if added else "−"
    draw_centered(draw, canvas.width, 158, title, font(20, True), silver)
    draw_centered(draw, canvas.width, 245, f"{sign}{amount:,}", font(112, True, True), white)
    draw_centered(draw, canvas.width, 390, "POINTS", font(18, True), dim)
    draw.line((410, 450, 990, 450), fill=(177, 180, 185, 48), width=1)
    draw_centered(draw, canvas.width, 510, member.display_name[:28], font(38, True, True), white)
    draw_centered(draw, canvas.width, 586, f"NEW BALANCE  /  {total:,}", font(22, True), silver)
    draw_centered(draw, canvas.width, 760, "D70  /  VERIFIED STAFF ACTION", font(15, True), dim)
    output = GENERATED_DIR / f"points_{member.id}_{'add' if added else 'remove'}.png"
    canvas.convert("RGB").save(output, "PNG", optimize=True)
    return output


def create_shortcut_image(alias: str, target: str, synced: int) -> Path:
    canvas = background(1400, 900)
    draw = ImageDraw.Draw(canvas)
    add_logo(canvas, draw)
    white = (232, 233, 235, 255)
    silver = (159, 162, 167, 255)
    dim = (91, 94, 100, 255)
    draw_centered(draw, canvas.width, 158, "SHORTCUT CREATED", font(20, True), silver)
    draw_centered(draw, canvas.width, 255, f"/{alias}", font(76, True, True), white)
    draw_centered(draw, canvas.width, 390, "ROUTES TO", font(16, True), dim)
    draw_centered(draw, canvas.width, 455, f"/{target}", font(43, True, True), silver)
    draw.line((430, 560, 970, 560), fill=(177, 180, 185, 42), width=1)
    draw_centered(draw, canvas.width, 620, f"{synced} COMMANDS SYNCHRONIZED", font(19, True), silver)
    draw_centered(draw, canvas.width, 760, "D70  /  COMMAND SYSTEM", font(15, True), dim)
    output = GENERATED_DIR / f"shortcut_{alias}.png"
    canvas.convert("RGB").save(output, "PNG", optimize=True)
    return output


async def send_image(interaction: discord.Interaction, path: Path) -> None:
    unique_name = (
        f"{path.stem}_{datetime.now(timezone.utc).strftime('%H%M%S%f')}.png"
    )
    file = discord.File(path, filename=unique_name)
    if interaction.response.is_done():
        await interaction.followup.send(file=file)
    else:
        await interaction.response.send_message(file=file)


async def defer_response(interaction: discord.Interaction) -> bool:
    if interaction.response.is_done():
        return True
    try:
        await interaction.response.defer(thinking=True)
        return True
    except discord.HTTPException as error:
        if error.code == 40060:
            logger.warning("Interaction was acknowledged by another running bot instance.")
            return False
        raise


async def send_notice(
    interaction: discord.Interaction,
    message: str,
    *,
    ephemeral: bool = True,
) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=ephemeral)
    else:
        await interaction.response.send_message(message, ephemeral=ephemeral)


async def get_member(guild: discord.Guild, value: str) -> discord.Member | None:
    member_id = extract_id(value)
    if member_id is None:
        return None
    member = guild.get_member(member_id)
    if member:
        return member
    try:
        return await guild.fetch_member(member_id)
    except discord.NotFound:
        return None


async def _apply_member_rank(
    guild_id: int,
    member_id: int,
    total: int,
    obsolete_role_ids: set[int] | None = None,
) -> None:
    guild = bot.get_guild(guild_id)
    if not guild:
        logger.warning("Rank sync skipped: guild %s is unavailable.", guild_id)
        return
    member = guild.get_member(member_id)
    if not member:
        try:
            member = await guild.fetch_member(member_id)
        except discord.NotFound:
            logger.warning("Rank sync skipped: member %s is unavailable.", member_id)
            return
    tiers = rank_tiers_for(guild_id)
    if not tiers and not obsolete_role_ids:
        return
    eligible = [tier for tier in tiers if total >= tier["points"]]
    desired_tier = eligible[-1] if eligible else None
    desired_role = (
        guild.get_role(int(desired_tier["role_id"]))
        if desired_tier is not None
        else None
    )
    configured_ids = {int(tier["role_id"]) for tier in tiers}
    configured_ids.update(obsolete_role_ids or set())
    bot_member = guild.me
    if desired_role and (
        desired_role.managed
        or (bot_member is not None and desired_role >= bot_member.top_role)
    ):
        logger.warning(
            "Cannot assign rank role %s in %s: move the bot role above it.",
            desired_role.name,
            guild.name,
        )
        return
    retained_roles = [
        role
        for role in member.roles
        if role != guild.default_role
        and (
            role.id not in configured_ids
            or role.managed
            or (bot_member is not None and role >= bot_member.top_role)
        )
    ]
    if desired_role and desired_role not in retained_roles:
        retained_roles.append(desired_role)
    current_ids = {
        role.id
        for role in member.roles
        if role != guild.default_role
    }
    target_ids = {role.id for role in retained_roles}
    if current_ids == target_ids:
        return
    try:
        await member.edit(
            roles=retained_roles,
            reason=f"D70 automatic staff rank sync at {total} points",
        )
        logger.info(
            "Automatic rank sync: %s -> %s (%s points)",
            member,
            desired_role.name if desired_role else "no configured rank",
            total,
        )
    except discord.Forbidden:
        logger.warning(
            "Automatic rank sync failed for %s: Manage Roles or role hierarchy is missing.",
            member,
        )
    except discord.HTTPException:
        logger.exception("Discord rejected automatic rank sync for %s.", member)


async def sync_member_rank(
    guild_id: int,
    member_id: int,
    obsolete_role_ids: set[int] | None = None,
) -> None:
    key = (guild_id, member_id)
    lock = RANK_SYNC_LOCKS.setdefault(key, asyncio.Lock())
    async with lock:
        current_total = points_for(member_id, guild_id)
        await _apply_member_rank(
            guild_id,
            member_id,
            current_total,
            obsolete_role_ids,
        )


def schedule_rank_sync(
    guild_id: int,
    member_id: int,
    obsolete_role_ids: set[int] | None = None,
) -> None:
    if not BOT_LOOP or not BOT_LOOP.is_running():
        logger.warning("Rank sync skipped because the Discord bot is not ready.")
        return
    future = asyncio.run_coroutine_threadsafe(
        sync_member_rank(guild_id, member_id, obsolete_role_ids),
        BOT_LOOP,
    )

    def log_rank_result(done: Any) -> None:
        try:
            done.result()
        except Exception:
            logger.exception("Automatic rank sync failed.")

    future.add_done_callback(log_rank_result)


def schedule_all_rank_syncs(
    guild_id: int,
    obsolete_role_ids: set[int] | None = None,
) -> None:
    store = guild_data(guild_id)
    guild = bot.get_guild(guild_id)
    member_ids = {
        str(member_id)
        for member_id in store["points"]
        if str(member_id).isdigit()
    }
    if guild:
        member_ids.update(str(member.id) for member in guild.members)
    for member_id in member_ids:
        if member_id.isdigit():
            schedule_rank_sync(
                guild_id,
                int(member_id),
                obsolete_role_ids,
            )


async def update_member_points(interaction: discord.Interaction, member: discord.Member,
                               amount: int, multiplier: int) -> None:
    if amount <= 0:
        await send_notice(interaction, "يجب أن تكون قيمة النقاط أكبر من صفر.")
        return
    if not valid_staff_role(member):
        await send_notice(
            interaction,
            f"العضو يجب أن يحمل رتبة `{setting('staff_role_name', guild_id=member.guild.id)}` حسب الإعدادات.",
        )
        return
    total = change_points(
        member.id,
        amount * multiplier,
        member.display_name,
        member.guild.id,
    )
    await sync_member_rank(member.guild.id, member.id)
    await send_image(
        interaction,
        create_points_action_image(member, amount, total, multiplier > 0),
    )


@bot.tree.command(name="addpoints", description="إضافة نقاط لعضو من الستاف")
async def addpoints(interaction: discord.Interaction, staff: discord.Member, amount: int) -> None:
    if not await defer_response(interaction):
        return
    if await require_manager(interaction):
        await update_member_points(interaction, staff, amount, 1)


@bot.tree.command(name="removepoints", description="خصم نقاط من عضو من الستاف")
async def removepoints(interaction: discord.Interaction, staff: discord.Member, amount: int) -> None:
    if not await defer_response(interaction):
        return
    if await require_manager(interaction):
        await update_member_points(interaction, staff, amount, -1)


@bot.tree.command(name="me", description="عرض بطاقة نقاطك وترتيبك")
async def me(interaction: discord.Interaction) -> None:
    if not await defer_response(interaction):
        return
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await send_notice(interaction, "هذا الأمر يعمل داخل السيرفر فقط.")
        return
    rows = leaderboard_rows(interaction.guild)
    rank = next((index for index, (member, _, _) in enumerate(rows, 1)
                 if member and member.id == interaction.user.id), len(rows) + 1)
    await send_image(interaction, create_profile_image(interaction.user, rank, len(rows)))


@bot.tree.command(name="leaderboard", description="عرض صورة لوحة صدارة الستاف")
async def leaderboard(interaction: discord.Interaction) -> None:
    if not await defer_response(interaction):
        return
    if not interaction.guild:
        await send_notice(interaction, "هذا الأمر يعمل داخل السيرفر فقط.")
        return
    rows = leaderboard_rows(interaction.guild)
    await send_image(interaction, create_leaderboard_image(interaction.guild, rows))


async def execute_shortcut(interaction: discord.Interaction, target: str, arguments: str) -> None:
    if target == "me":
        await me.callback(interaction)
        return
    if target == "leaderboard":
        await leaderboard.callback(interaction)
        return
    if not await require_manager(interaction, "points"):
        return
    if not interaction.guild:
        await send_notice(interaction, "هذا الأمر يعمل داخل السيرفر فقط.")
        return
    try:
        values = shlex.split(arguments) if arguments.strip() else []
    except ValueError:
        await send_notice(interaction, "صيغة arguments غير صحيحة.")
        return
    if len(values) != 2:
        await send_notice(interaction, "هذا الاختصار يحتاج arguments بهذا الشكل: `@staff amount`.")
        return
    member = await get_member(interaction.guild, values[0])
    if not member:
        await send_notice(interaction, "لم أجد العضو. استخدم منشن أو ID.")
        return
    try:
        amount = int(values[1])
    except ValueError:
        await send_notice(interaction, "amount يجب أن يكون رقمًا صحيحًا.")
        return
    await update_member_points(interaction, member, amount, 1 if target == "addpoints" else -1)


def make_alias_callback(alias: str) -> Callable[..., Any]:
    async def alias_callback(interaction: discord.Interaction, arguments: str = "") -> None:
        if not await defer_response(interaction):
            return
        if not interaction.guild_id:
            await send_notice(interaction, "هذا الأمر يعمل داخل السيرفر فقط.")
            return
        target = resolve_target(alias, interaction.guild_id)
        if not target:
            await send_notice(interaction, "هذا الاختصار غير مفعّل في هذا السيرفر.")
            return
        await execute_shortcut(interaction, target, arguments)
    return alias_callback


def register_shortcuts() -> None:
    for name in list(REGISTERED_SHORTCUTS):
        bot.tree.remove_command(name)
    REGISTERED_SHORTCUTS.clear()
    aliases: set[str] = set()
    for store in DATA.get("guilds", {}).values():
        if isinstance(store, dict) and isinstance(store.get("shortcuts"), dict):
            aliases.update(str(name).lower() for name in store["shortcuts"])
    for alias in sorted(aliases):
        name = str(alias).lower()
        if name in BUILTIN_COMMANDS:
            continue
        bot.tree.add_command(app_commands.Command(
            name=name,
            description="اختصار مخصص حسب إعدادات السيرفر",
            callback=make_alias_callback(name),
        ))
        REGISTERED_SHORTCUTS.add(name)


async def sync_commands() -> int:
    register_shortcuts()
    synced = await bot.tree.sync()
    return len(synced)


@bot.tree.command(name="shortcut", description="إنشاء اختصار لأمر موجود")
async def shortcut(interaction: discord.Interaction, command_name: str, target_command: str) -> None:
    if not await defer_response(interaction):
        return
    if not await require_manager(interaction, "shortcuts"):
        return
    if not interaction.guild_id:
        await send_notice(interaction, "هذا الأمر يعمل داخل السيرفر فقط.")
        return
    try:
        alias = clean_command_name(command_name)
    except ValueError as error:
        await send_notice(interaction, str(error))
        return
    target = resolve_target(target_command, interaction.guild_id)
    if not target:
        await send_notice(
            interaction,
            "الأمر الهدف غير موجود. استخدم: " + ", ".join(f"`/{x}`" for x in sorted(BUILTIN_COMMANDS - {"shortcut"})),
        )
        return
    if alias == target:
        await send_notice(interaction, "لا يمكن أن يكون الاختصار للأمر نفسه.")
        return
    guild_data(interaction.guild_id)["shortcuts"][alias] = target
    save_data()
    synced = await sync_commands()
    await send_image(interaction, create_shortcut_image(alias, target, synced))


@bot.event
async def on_ready() -> None:
    global BOT_LOOP
    BOT_LOOP = asyncio.get_running_loop()
    synced = await sync_commands()
    logger.info("Logged in as %s", bot.user)
    logger.info("Slash commands synced: %s", synced)


@bot.tree.error
async def command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    logger.exception("Slash command error: %s", error)
    message = "حدث خطأ غير متوقع أثناء تنفيذ الأمر."
    if isinstance(error, app_commands.TransformerError):
        message = "تأكد من أن القيم المدخلة صحيحة."
    try:
        await send_notice(interaction, message)
    except discord.HTTPException as response_error:
        if response_error.code != 40060:
            raise


# -----------------------------------------------------------------------------
# Protected Flask dashboard
# -----------------------------------------------------------------------------

dashboard = Flask(__name__)
dashboard.config["SECRET_KEY"] = os.getenv("SESSION_SECRET", secrets.token_hex(32))
dashboard.config["SESSION_COOKIE_HTTPONLY"] = True
dashboard.config["SESSION_COOKIE_SAMESITE"] = "Lax"
PORT = int(os.getenv("PORT", "8080"))
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "g6ibvskb")
logger.warning("Dashboard access code is configured.")


def dashboard_session_valid() -> bool:
    if not session.get("authenticated"):
        return False
    if session.get("dashboard_role") == "owner":
        return True
    user_id = str(session.get("dashboard_user_id", ""))
    return user_id.isdigit() and is_bot_admin(user_id)


def dashboard_is_owner() -> bool:
    return dashboard_session_valid() and session.get("dashboard_role") == "owner"


def dashboard_can(permission: str) -> bool:
    if dashboard_is_owner():
        return True
    return permission in bot_admin_permissions(str(session.get("dashboard_user_id", "")))


STYLE = """
<style>
:root{color-scheme:dark;--red:{{accent}};--bg:#06070a;--panel:#111319;--soft:#171a22;--line:#282c36;--muted:#9298a8}
*{box-sizing:border-box}body{margin:0;min-height:100vh;background:radial-gradient(circle at 90% -10%,color-mix(in srgb,var(--red) 28%,transparent),transparent 32%),var(--bg);color:#f6f7f9;font-family:Tahoma,Arial,sans-serif}
a{text-decoration:none;color:inherit}.shell{max-width:1380px;margin:auto;padding:24px 20px 70px}.top{display:flex;justify-content:space-between;align-items:center;gap:20px;margin-bottom:22px}
.brand{display:flex;align-items:center;gap:13px}.brand img{width:52px;height:52px;object-fit:cover;border-radius:15px;border:1px solid #ffffff25;background:#777}.brand strong{font-size:17px}.eyebrow{color:var(--red);font-size:11px;font-weight:900;letter-spacing:.12em}.muted{color:var(--muted)}
.logout{padding:10px 14px;border:1px solid var(--line);border-radius:12px;color:#d9dce3}.workspace{display:grid;grid-template-columns:235px minmax(0,1fr);gap:20px;align-items:start}
.sidebar{position:sticky;top:20px;background:#0d0f14dd;border:1px solid var(--line);border-radius:22px;padding:14px}.server-chip{display:flex;gap:11px;align-items:center;background:var(--soft);border:1px solid var(--line);border-radius:15px;padding:12px;margin-bottom:12px}
.server-avatar,.server-fallback{width:42px;height:42px;border-radius:13px;object-fit:cover}.server-fallback{display:grid;place-items:center;background:var(--red);font-weight:900}.server-chip strong{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:135px}.server-chip small{color:var(--muted)}
.nav{display:grid;gap:6px}.nav a{padding:12px 13px;border-radius:12px;color:#b8bdc9;font-weight:700}.nav a:hover,.nav a.active{background:color-mix(in srgb,var(--red) 20%,#151820);color:#fff}.switch{display:block;margin-top:12px;padding:11px;text-align:center;border-top:1px solid var(--line);color:#ff8b91;font-size:13px}
.content{min-width:0}.page-head{display:flex;justify-content:space-between;gap:18px;align-items:end;margin-bottom:20px}.page-head h1{font-size:clamp(28px,5vw,44px);letter-spacing:-.04em;margin:5px 0}.page-head p{margin:0}
.grid{display:grid;grid-template-columns:repeat(12,1fr);gap:16px}.card{background:linear-gradient(145deg,#161922ed,#0d0f14ed);border:1px solid var(--line);border-radius:20px;padding:21px;box-shadow:0 22px 60px #0005}.wide{grid-column:span 8}.side{grid-column:span 4}.half{grid-column:span 6}.full{grid-column:1/-1}
.stat-card{position:relative;overflow:hidden}.stat-card:after{content:"";position:absolute;width:90px;height:90px;border-radius:50%;background:var(--red);filter:blur(50px);opacity:.2;left:-20px;bottom:-30px}.stat{font-size:38px;font-weight:900;margin-top:8px}.label{font-size:13px;color:var(--muted)}
.quick{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.quick a{padding:18px;border:1px solid var(--line);border-radius:16px;background:#0b0d12}.quick strong{display:block;margin-bottom:7px}.quick span{color:var(--muted);font-size:13px}
h1{font-size:clamp(28px,5vw,46px);letter-spacing:-.05em;margin:7px 0 22px}h2{font-size:19px;margin:0 0 17px}label{display:block;font-size:13px;color:var(--muted);margin:12px 0 6px}
input,select{width:100%;border:1px solid var(--line);border-radius:12px;background:#080a0e;color:#fff;padding:12px 13px;font:inherit;outline:none}input:focus,select:focus{border-color:var(--red)}input[type=checkbox]{width:auto;accent-color:var(--red)}
button,.button{display:inline-block;border:0;border-radius:11px;background:var(--red);color:#fff;font-weight:900;padding:12px 16px;cursor:pointer;margin-top:14px;font:inherit}.button.ghost{background:#20242d}.danger{background:#75232b!important}
table{width:100%;border-collapse:collapse}th,td{text-align:right;padding:13px 8px;border-bottom:1px solid var(--line);font-size:14px}th{color:var(--muted);font-size:12px}.row{display:flex;gap:11px;align-items:end}.row>*{flex:1}.notice{padding:13px 15px;border-radius:12px;background:#163022;color:#c8f4d3;margin-bottom:18px}.error{background:#3d171d;color:#ffb8bd}
.login{max-width:480px;margin:10vh auto}.guilds{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px}.guild-card{background:linear-gradient(145deg,#161922,#0d0f14);border:1px solid var(--line);border-radius:20px;padding:18px}.guild-head{display:flex;align-items:center;gap:12px}.guild-card button{width:100%}
.owner{display:flex;gap:12px;align-items:center;padding:14px;border:1px solid color-mix(in srgb,var(--red) 45%,var(--line));background:color-mix(in srgb,var(--red) 9%,#111319);border-radius:15px}.empty{padding:35px;text-align:center;color:var(--muted)}
.checks{display:flex;flex-wrap:wrap;gap:9px;margin-top:11px}.check{display:flex;align-items:center;gap:7px;padding:9px 11px;border:1px solid var(--line);border-radius:11px;background:#090b0f;color:#d9dce3}.badge{display:inline-block;padding:5px 9px;border-radius:999px;background:#20242d;color:#c9ced8;font-size:11px}.badge.senior{background:color-mix(in srgb,var(--red) 25%,#20242d);color:#fff}.secret-code{font-family:monospace;font-size:22px;direction:ltr;text-align:center;letter-spacing:.12em;background:#080a0e;border:1px dashed #ffffff55;padding:14px;border-radius:12px}.admin-card{display:grid;gap:13px}
@media(max-width:900px){.workspace{grid-template-columns:1fr}.sidebar{position:static}.nav{grid-template-columns:repeat(4,1fr)}.nav a{text-align:center;padding:10px 5px;font-size:12px}.switch{border-top:0}.wide,.side,.half{grid-column:1/-1}.quick{grid-template-columns:1fr}}
@media(max-width:600px){.shell{padding:16px 12px 50px}.top{align-items:flex-start}.brand img{width:44px;height:44px}.row{display:grid}.page-head{display:block}.nav{grid-template-columns:repeat(2,1fr)}table{display:block;overflow:auto}.card{padding:17px}}
</style>
"""

LAYOUT = """<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{{title}}</title>""" + STYLE + """</head>
<body><main class="shell"><header class="top"><div class="brand"><img src="{{url_for('logo')}}" alt="D70 logo">
<div><div class="eyebrow">D70 STAFF OPERATIONS</div><strong>{{title}}</strong></div></div>
{% if authenticated %}<a class="logout" href="{{url_for('logout')}}">تسجيل الخروج</a>{% endif %}</header>
{% if authenticated and selected_guild %}<div class="workspace"><aside class="sidebar">
<div class="server-chip">{% if selected_guild.icon %}<img class="server-avatar" src="{{selected_guild.icon}}" alt="">{% else %}<div class="server-fallback">{{selected_guild.name[:1]}}</div>{% endif %}
<div><strong>{{selected_guild.name}}</strong><small>السيرفر الحالي</small></div></div>
<nav class="nav"><a class="{{'active' if active=='home' else ''}}" href="{{url_for('admin')}}">الرئيسية</a>
{% if can_points %}<a class="{{'active' if active=='points' else ''}}" href="{{url_for('points_page')}}">النقاط</a>{% endif %}
{% if can_shortcuts %}<a class="{{'active' if active=='shortcuts' else ''}}" href="{{url_for('shortcuts_page')}}">الاختصارات</a>{% endif %}
{% if can_ranks %}<a class="{{'active' if active=='ranks' else ''}}" href="{{url_for('ranks_page')}}">الرتب الإدارية</a>{% endif %}
{% if can_settings %}<a class="{{'active' if active=='settings' else ''}}" href="{{url_for('settings_page')}}">الإعدادات</a>{% endif %}
{% if is_owner %}<a class="{{'active' if active=='bot_admins' else ''}}" href="{{url_for('bot_admins_page')}}">إدمنز البوت</a>{% endif %}</nav>
<a class="switch" href="{{url_for('servers_page')}}">تبديل السيرفر</a></aside><section class="content">{{body|safe}}</section></div>
{% else %}{{body|safe}}{% endif %}</main></body></html>"""

LOGIN_BODY = """<section class="card login"><div class="muted">D70 STAFF OPERATIONS</div><h1>تسجيل الدخول</h1>
{% if error %}<div class="notice error">{{error}}</div>{% endif %}
<form method="post" action="{{url_for('login')}}">
<label>الرمز السري</label><input type="password" name="password" placeholder="أدخل الرمز السري" required autofocus>
<button type="submit">دخول إلى اللوحة</button></form></section>"""


def page(body: str, **context: Any) -> str:
    active = str(context.pop("active", ""))
    guild = selected_guild()
    selected = guild_info(guild) if guild else None
    accent = setting("accent_color", "#e51e2a", guild.id if guild else None)
    title = setting(
        "dashboard_title",
        "D70 Staff Operations",
        guild.id if guild else None,
    )
    rendered_body = render_template_string(body, **context)
    return render_template_string(
        LAYOUT, body=rendered_body, title=title,
        authenticated=dashboard_session_valid(), accent=accent,
        selected_guild=selected, active=active,
        is_owner=dashboard_is_owner(),
        can_points=dashboard_can("points"),
        can_shortcuts=dashboard_can("shortcuts"),
        can_settings=dashboard_can("settings"),
        can_ranks=dashboard_can("ranks"),
    )


def login_required(view: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        return view(*args, **kwargs) if dashboard_session_valid() else redirect(url_for("login"))
    return wrapped


def owner_required(view: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        if not dashboard_is_owner():
            return redirect(url_for("admin", notice="هذه الصفحة متاحة للمالك فقط."))
        return view(*args, **kwargs)
    return wrapped


def permission_required(permission: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(view: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(view)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            if not dashboard_session_valid():
                return redirect(url_for("login"))
            if not dashboard_can(permission):
                return redirect(url_for("admin", notice="لا تملك صلاحية الوصول لهذا القسم."))
            return view(*args, **kwargs)
        return wrapped
    return decorator


def selected_guild() -> discord.Guild | None:
    raw = str(session.get("selected_guild_id", ""))
    return bot.get_guild(int(raw)) if raw.isdigit() else None


def guild_info(guild: discord.Guild) -> dict[str, Any]:
    return {
        "id": str(guild.id),
        "name": guild.name,
        "members": guild.member_count or len(guild.members),
        "icon": str(guild.icon.url) if guild.icon else "",
    }


def guild_required(view: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        if not dashboard_session_valid():
            return redirect(url_for("login"))
        if not selected_guild():
            session.pop("selected_guild_id", None)
            return redirect(url_for("servers_page"))
        return view(*args, **kwargs)
    return wrapped


@dashboard.get("/logo.png")
def logo() -> Any:
    return send_file(LOGO_PATH) if LOGO_PATH.exists() else ("", 404)


@dashboard.route("/", methods=["GET", "POST"])
def home() -> Any:
    if request.method == "POST":
        return login()
    if dashboard_session_valid():
        return redirect(url_for("admin") if selected_guild() else url_for("servers_page"))
    return login()


@dashboard.get("/healthz")
def healthz() -> tuple[str, int]:
    return "ok", 200


@dashboard.get("/favicon.ico")
def favicon() -> Any:
    return send_file(LOGO_PATH) if LOGO_PATH.exists() else ("", 204)


@dashboard.errorhandler(404)
def not_found(_: Any) -> Any:
    return redirect(url_for("home"))


@dashboard.errorhandler(405)
def method_not_allowed(_: Any) -> Any:
    return redirect(url_for("home"))


@dashboard.route("/login", methods=["GET", "POST"])
def login() -> Any:
    error = ""
    if request.method == "POST":
        password = request.form.get("password", "")
        if hmac.compare_digest(password, DASHBOARD_PASSWORD):
            session.clear()
            session["authenticated"] = True
            session["dashboard_role"] = "owner"
            session["dashboard_user_id"] = OWNER_USER_ID or "owner"
            return redirect(url_for("servers_page"))
        error = "الرمز السري غير صحيح."
    return page(LOGIN_BODY, error=error)


@dashboard.get("/logout")
def logout() -> Any:
    session.clear()
    return redirect(url_for("login"))


@dashboard.get("/servers")
@login_required
def servers_page() -> Any:
    guilds = [guild_info(guild) for guild in sorted(bot.guilds, key=lambda item: item.name.casefold())]
    body = """<div class="page-head"><div><div class="eyebrow">الخطوة الأولى</div><h1>اختر السيرفر</h1>
<p class="muted">كل سيرفر يملك نقاطه واختصاراته وإعداداته بشكل مستقل.</p></div></div>
{% if guilds %}<div class="guilds">{% for guild in guilds %}<article class="guild-card"><div class="guild-head">
{% if guild.icon %}<img class="server-avatar" src="{{guild.icon}}" alt="">{% else %}<div class="server-fallback">{{guild.name[:1]}}</div>{% endif %}
<div><strong>{{guild.name}}</strong><div class="muted">{{guild.members}} عضو</div></div></div>
<form method="post" action="{{url_for('select_server')}}"><input type="hidden" name="guild_id" value="{{guild.id}}">
<button type="submit">إدارة هذا السيرفر</button></form></article>{% endfor %}</div>
{% else %}<section class="card empty">البوت لا يظهر في أي سيرفر حاليًا. انتظر اتصاله ثم حدّث الصفحة.</section>{% endif %}"""
    return page(body, guilds=guilds)


@dashboard.post("/servers/select")
@login_required
def select_server() -> Any:
    raw = request.form.get("guild_id", "")
    guild = bot.get_guild(int(raw)) if raw.isdigit() else None
    if not guild:
        return redirect(url_for("servers_page"))
    session["selected_guild_id"] = str(guild.id)
    guild_data(guild.id)
    return redirect(url_for("admin"))


@dashboard.get("/admin")
@guild_required
def admin() -> Any:
    guild = selected_guild()
    assert guild is not None
    store = guild_data(guild.id)
    total_points = sum(points_for(member_id, guild.id) for member_id in store["points"])
    body = """{% if notice %}<div class="notice">{{notice}}</div>{% endif %}
<div class="page-head"><div><div class="eyebrow">لوحة التحكم</div><h1>{{guild.name}}</h1>
<p class="muted">ملخص سريع لإدارة نظام نقاط الستاف.</p></div></div>
<div class="grid"><article class="card stat-card side"><div class="label">سجلات الأعضاء</div><div class="stat">{{members}}</div></article>
<article class="card stat-card side"><div class="label">إجمالي النقاط</div><div class="stat">{{"{:,}".format(total_points)}}</div></article>
<article class="card stat-card side"><div class="label">الاختصارات</div><div class="stat">{{shortcuts}}</div></article>
<section class="card full"><h2>ماذا تريد أن تعدّل؟</h2><div class="quick">
{% if can_points %}<a href="{{url_for('points_page')}}"><strong>إدارة النقاط</strong><span>إضافة وتعديل وحذف أرصدة الأعضاء.</span></a>{% endif %}
{% if can_shortcuts %}<a href="{{url_for('shortcuts_page')}}"><strong>إدارة الاختصارات</strong><span>إنشاء أوامر مختصرة خاصة بهذا السيرفر.</span></a>{% endif %}
{% if can_ranks %}<a href="{{url_for('ranks_page')}}"><strong>الرتب الإدارية</strong><span>رتب صغرى وعليا وترقية تلقائية بالنقاط.</span></a>{% endif %}
{% if can_settings %}<a href="{{url_for('settings_page')}}"><strong>إعدادات السيرفر</strong><span>اسم السيرفر والرتبة واللون الرئيسي.</span></a>{% endif %}
{% if is_owner %}<a href="{{url_for('bot_admins_page')}}"><strong>إدمنز البوت</strong><span>إضافة الإدمنز وتحديد صلاحياتهم.</span></a>{% endif %}</div></section>
{% if is_owner %}
<section class="card full owner"><div class="server-fallback">★</div><div><strong>حساب المالك مفعّل</strong>
<div class="muted">Discord ID: {{owner_id}}</div></div></section>{% endif %}</div>"""
    return page(
        body,
        active="home",
        guild=guild_info(guild),
        members=len(store["points"]),
        total_points=total_points,
        shortcuts=len(store["shortcuts"]),
        owner_id=OWNER_USER_ID,
        is_owner=dashboard_is_owner(),
        can_points=dashboard_can("points"),
        can_shortcuts=dashboard_can("shortcuts"),
        can_settings=dashboard_can("settings"),
        can_ranks=dashboard_can("ranks"),
        notice=request.args.get("notice", ""),
    )


def point_records(guild: discord.Guild) -> list[dict[str, Any]]:
    store = guild_data(guild.id)
    records = []
    for member_id in store["points"]:
        member = guild.get_member(int(member_id)) if str(member_id).isdigit() else None
        records.append({
            "id": str(member_id),
            "name": member.display_name if member else store["members"].get(str(member_id), "عضو غير معروف"),
            "points": points_for(member_id, guild.id),
        })
    return sorted(records, key=lambda row: (-row["points"], row["name"].casefold()))


@dashboard.get("/admin/points")
@guild_required
@permission_required("points")
def points_page() -> Any:
    guild = selected_guild()
    assert guild is not None
    body = """{% if notice %}<div class="notice">{{notice}}</div>{% endif %}
<div class="page-head"><div><div class="eyebrow">إدارة الأعضاء</div><h1>النقاط</h1><p class="muted">تعديل أرصدة سيرفر {{guild.name}} فقط.</p></div></div>
<div class="grid"><section class="card full"><h2>تعديل رصيد عضو</h2><form method="post" action="{{url_for('update_points')}}"><div class="row">
<div><label>Discord ID أو منشن</label><input name="member_id" placeholder="123456789012345678" required></div>
<div><label>الاسم الظاهر (اختياري)</label><input name="display_name" placeholder="Staff member"></div>
<div><label>الرصيد الجديد</label><input type="number" min="0" name="points" required></div></div><button>حفظ الرصيد</button></form></section>
<section class="card full"><h2>سجلات الأعضاء</h2>{% if records %}<table><thead><tr><th>العضو</th><th>ID</th><th>النقاط</th><th></th></tr></thead><tbody>
{% for row in records %}<tr><td>{{row.name}}</td><td class="muted">{{row.id}}</td><td>{{"{:,}".format(row.points)}}</td><td><form method="post" action="{{url_for('delete_points')}}"><input type="hidden" name="member_id" value="{{row.id}}"><button class="danger">حذف</button></form></td></tr>{% endfor %}
</tbody></table>{% else %}<div class="empty">لا توجد سجلات نقاط في هذا السيرفر بعد.</div>{% endif %}</section></div>"""
    return page(body, active="points", guild=guild_info(guild), records=point_records(guild), notice=request.args.get("notice", ""))


@dashboard.get("/admin/shortcuts")
@guild_required
@permission_required("shortcuts")
def shortcuts_page() -> Any:
    guild = selected_guild()
    assert guild is not None
    shortcuts = sorted(guild_data(guild.id)["shortcuts"].items())
    body = """{% if notice %}<div class="notice">{{notice}}</div>{% endif %}
<div class="page-head"><div><div class="eyebrow">أوامر أسرع</div><h1>الاختصارات</h1><p class="muted">اختصارات مستقلة لسيرفر {{guild.name}}.</p></div></div>
<div class="grid"><section class="card half"><h2>إنشاء اختصار</h2><form method="post" action="{{url_for('create_shortcut')}}">
<label>اسم الاختصار بالإنجليزية</label><input name="command_name" placeholder="topstaff" required><label>الأمر الهدف</label>
<select name="target_command">{% for command in commands %}<option value="{{command}}">/{{command}}</option>{% endfor %}</select><button>إنشاء ومزامنة</button></form></section>
<section class="card half"><h2>الاختصارات الحالية</h2>{% for name,target in shortcuts %}<div class="row" style="margin:9px 0">
<span><strong>/{{name}}</strong><div class="muted">ينفذ /{{target}}</div></span><form method="post" action="{{url_for('delete_shortcut')}}"><input type="hidden" name="command_name" value="{{name}}"><button class="danger">حذف</button></form></div>
{% else %}<div class="empty">لا توجد اختصارات لهذا السيرفر.</div>{% endfor %}</section></div>"""
    return page(body, active="shortcuts", guild=guild_info(guild), shortcuts=shortcuts,
                commands=sorted(BUILTIN_COMMANDS - {"shortcut"}), notice=request.args.get("notice", ""))


@dashboard.get("/admin/settings")
@guild_required
@permission_required("settings")
def settings_page() -> Any:
    guild = selected_guild()
    assert guild is not None
    settings = guild_data(guild.id)["settings"]
    body = """{% if notice %}<div class="notice">{{notice}}</div>{% endif %}
<div class="page-head"><div><div class="eyebrow">تخصيص التجربة</div><h1>الإعدادات</h1><p class="muted">هذه الإعدادات تخص {{guild.name}} فقط.</p></div></div>
<div class="grid"><section class="card wide"><h2>إعدادات السيرفر</h2><form method="post" action="{{url_for('update_settings')}}">
<label>عنوان لوحة التحكم</label><input name="dashboard_title" value="{{settings.dashboard_title}}">
<label>اسم السيرفر داخل الصور</label><input name="server_name" value="{{settings.server_name}}" placeholder="{{guild.name}}">
<label>اسم رتبة الستاف المطلوبة (اختياري)</label><input name="staff_role_name" value="{{settings.staff_role_name}}" placeholder="Staff">
<label>اللون الرئيسي</label><input type="color" name="accent_color" value="{{settings.accent_color}}">
<button>حفظ الإعدادات</button></form></section><aside class="card side"><h2>المالك</h2><div class="owner"><div class="server-fallback">★</div>
<div><strong>صلاحية المالك مفعّلة</strong><div class="muted">{{owner_id}}</div></div></div>
<p class="muted">حسابك يستطيع استخدام أوامر الإدارة حتى بدون رتبة إدارية.</p></aside></div>"""
    return page(body, active="settings", guild=guild_info(guild), settings=settings,
                owner_id=OWNER_USER_ID, notice=request.args.get("notice", ""))


def configurable_roles(guild: discord.Guild) -> list[dict[str, Any]]:
    bot_member = guild.me
    return [
        {
            "id": str(role.id),
            "name": role.name,
            "manageable": bool(
                bot_member
                and bot_member.guild_permissions.manage_roles
                and role < bot_member.top_role
            ),
        }
        for role in sorted(guild.roles, key=lambda item: item.position)
        if role != guild.default_role and not role.managed
    ]


@dashboard.get("/admin/ranks")
@guild_required
@permission_required("ranks")
def ranks_page() -> Any:
    guild = selected_guild()
    assert guild is not None
    tiers = rank_tiers_for(guild.id)
    configured_ids = {tier["role_id"] for tier in tiers}
    roles = configurable_roles(guild)
    body = """{% if notice %}<div class="notice">{{notice}}</div>{% endif %}
<div class="page-head"><div><div class="eyebrow">نظام الترقيات</div><h1>الرتب الإدارية</h1>
<p class="muted">قسّم الرتب إلى صغرى وعليا وحدد نقاط الوصول. الترتيب يتحدث تلقائيًا من الأقل نقاطًا إلى الأعلى.</p></div></div>
<div class="grid"><section class="card full"><h2>إضافة رتبة للمسار</h2>
<form method="post" action="{{url_for('add_rank_tier')}}"><div class="row">
<div><label>رتبة Discord</label><select name="role_id" required>
{% for role in roles if role.id not in configured_ids %}<option value="{{role.id}}">{{role.name}}{{'' if role.manageable else ' — ارفع رتبة البوت فوقها'}}</option>{% endfor %}
</select></div><div><label>القسم</label><select name="category"><option value="junior">إدارة صغرى</option><option value="senior">إدارة عليا</option></select></div>
<div><label>النقاط المطلوبة</label><input type="number" min="0" name="points" value="0" required></div></div>
<button>إضافة الرتبة</button></form>
<p class="muted">يجب أن يملك البوت صلاحية Manage Roles وأن تكون رتبته أعلى من جميع الرتب التي سيمنحها.</p></section>
{% for title,category in [('الإدارة الصغرى','junior'),('الإدارة العليا','senior')] %}
<section class="card half"><h2>{{title}}</h2>
{% for tier in tiers if tier.category==category %}<article class="admin-card" style="border-bottom:1px solid var(--line);padding:12px 0">
<div><strong>{{tier.name}}</strong> <span class="badge {{'senior' if category=='senior' else ''}}">{{"{:,}".format(tier.points)}} نقطة</span>
<div class="muted">Role ID: {{tier.role_id}}</div></div>
<form method="post" action="{{url_for('update_rank_tier')}}"><input type="hidden" name="role_id" value="{{tier.role_id}}"><div class="row">
<div><label>القسم</label><select name="category"><option value="junior" {{'selected' if tier.category=='junior' else ''}}>صغرى</option><option value="senior" {{'selected' if tier.category=='senior' else ''}}>عليا</option></select></div>
<div><label>نقاط الترقية</label><input type="number" min="0" name="points" value="{{tier.points}}" required></div><div><button>حفظ</button></div></div></form>
<form method="post" action="{{url_for('delete_rank_tier')}}"><input type="hidden" name="role_id" value="{{tier.role_id}}"><button class="danger">إزالة من المسار</button></form>
</article>{% else %}<div class="empty">لم تضف رتبًا في هذا القسم.</div>{% endfor %}</section>{% endfor %}</div>"""
    return page(
        body,
        active="ranks",
        guild=guild_info(guild),
        tiers=tiers,
        roles=roles,
        configured_ids=configured_ids,
        notice=request.args.get("notice", ""),
    )


def discord_name_for(user_id: str) -> str:
    user = bot.get_user(int(user_id)) if user_id.isdigit() else None
    if user:
        return user.global_name or user.name
    for guild in bot.guilds:
        member = guild.get_member(int(user_id)) if user_id.isdigit() else None
        if member:
            return member.display_name
    return f"Discord User {user_id}"


def bot_admin_rows() -> list[dict[str, Any]]:
    rows = []
    with DATA_LOCK:
        admin_items = json.loads(json.dumps(list(DATA.get("bot_admins", {}).items())))
    for user_id, raw in admin_items:
        if not isinstance(raw, dict) or not str(user_id).isdigit():
            continue
        permissions = bot_admin_permissions(user_id)
        rows.append({
            "id": str(user_id),
            "name": discord_name_for(str(user_id)),
            "permissions": permissions,
        })
    return sorted(rows, key=lambda row: row["name"].casefold())


@dashboard.get("/admin/bot-admins")
@login_required
@owner_required
def bot_admins_page() -> Any:
    body = """{% if notice %}<div class="notice">{{notice}}</div>{% endif %}
<div class="page-head"><div><div class="eyebrow">إدارة البوت</div><h1>إدمنز البوت</h1>
<p class="muted">أضف Discord ID وحدد صلاحيات أوامر وإدارة البوت لهذا الحساب.</p></div></div>
<div class="grid"><section class="card full"><h2>إضافة أدمن بوت</h2><form method="post" action="{{url_for('add_bot_admin')}}">
<label>Discord User ID</label><input name="user_id" inputmode="numeric" placeholder="123456789012345678" required>
<div class="checks">{% for key,label in permission_labels.items() %}<label class="check"><input type="checkbox" name="permissions" value="{{key}}">{{label}}</label>{% endfor %}</div>
<button>إضافة الأدمن</button></form></section>
{% for admin in admins %}<section class="card half admin-card"><div><div class="eyebrow">BOT ADMIN</div><h2 style="margin:5px 0">{{admin.name}}</h2><div class="muted">{{admin.id}}</div></div>
<form method="post" action="{{url_for('update_bot_admin')}}"><input type="hidden" name="user_id" value="{{admin.id}}">
<div class="checks">{% for key,label in permission_labels.items() %}<label class="check"><input type="checkbox" name="permissions" value="{{key}}" {{'checked' if key in admin.permissions else ''}}>{{label}}</label>{% endfor %}</div>
<button>حفظ الصلاحيات</button></form>
<form method="post" action="{{url_for('delete_bot_admin')}}"><input type="hidden" name="user_id" value="{{admin.id}}"><button class="danger">حذف الأدمن</button></form></section>
{% else %}<section class="card full empty">لا يوجد إدمنز للبوت حتى الآن.</section>{% endfor %}</div>"""
    return page(
        body,
        active="bot_admins",
        admins=bot_admin_rows(),
        permission_labels=DASHBOARD_PERMISSION_LABELS,
        notice=request.args.get("notice", ""),
    )


def dashboard_id(value: str) -> str | None:
    parsed = extract_id(value)
    return str(parsed) if parsed is not None else (value.strip() if value.strip().isdigit() else None)


def submitted_permissions() -> list[str]:
    return sorted(set(request.form.getlist("permissions")) & DASHBOARD_PERMISSIONS)


@dashboard.post("/admin/bot-admins/add")
@login_required
@owner_required
def add_bot_admin() -> Any:
    user_id = dashboard_id(request.form.get("user_id", ""))
    if not user_id:
        return redirect(url_for("bot_admins_page", notice="Discord ID غير صحيح."))
    if OWNER_USER_ID and user_id == OWNER_USER_ID:
        return redirect(url_for("bot_admins_page", notice="هذا الحساب هو المالك بالفعل."))
    with DATA_LOCK:
        admins = DATA.setdefault("bot_admins", {})
        if user_id in admins:
            return redirect(url_for("bot_admins_page", notice="هذا الحساب مضاف مسبقًا."))
        admins[user_id] = {
            "name": discord_name_for(user_id),
            "permissions": submitted_permissions(),
        }
        save_data()
    return redirect(url_for("bot_admins_page", notice="تمت إضافة أدمن البوت."))


@dashboard.post("/admin/bot-admins/update")
@login_required
@owner_required
def update_bot_admin() -> Any:
    user_id = dashboard_id(request.form.get("user_id", ""))
    with DATA_LOCK:
        entry = DATA.get("bot_admins", {}).get(user_id or "")
        if not isinstance(entry, dict):
            return redirect(url_for("bot_admins_page", notice="الأدمن غير موجود."))
        entry["permissions"] = submitted_permissions()
        entry["name"] = discord_name_for(user_id or "")
        save_data()
    return redirect(url_for("bot_admins_page", notice="تم حفظ صلاحيات الأدمن."))


@dashboard.post("/admin/bot-admins/delete")
@login_required
@owner_required
def delete_bot_admin() -> Any:
    user_id = dashboard_id(request.form.get("user_id", ""))
    if user_id:
        with DATA_LOCK:
            DATA.get("bot_admins", {}).pop(user_id, None)
            save_data()
    return redirect(url_for("bot_admins_page", notice="تم حذف أدمن البوت."))


def requested_rank_values(guild: discord.Guild) -> tuple[discord.Role | None, str, int]:
    role_id = dashboard_id(request.form.get("role_id", ""))
    role = guild.get_role(int(role_id)) if role_id else None
    category = "senior" if request.form.get("category") == "senior" else "junior"
    try:
        required_points = max(0, int(request.form.get("points", "0")))
    except ValueError:
        required_points = 0
    return role, category, required_points


def rank_role_problem(guild: discord.Guild, role: discord.Role | None) -> str:
    if not role or role == guild.default_role or role.managed:
        return "رتبة Discord غير صالحة."
    bot_member = guild.me
    if not bot_member or not bot_member.guild_permissions.manage_roles:
        return "امنح البوت صلاحية Manage Roles أولًا."
    if role >= bot_member.top_role:
        return "ارفع رتبة البوت فوق هذه الرتبة حتى يستطيع منحها تلقائيًا."
    return ""


@dashboard.post("/admin/ranks/add")
@guild_required
@permission_required("ranks")
def add_rank_tier() -> Any:
    guild = selected_guild()
    assert guild is not None
    role, category, required_points = requested_rank_values(guild)
    problem = rank_role_problem(guild, role)
    if problem:
        return redirect(url_for("ranks_page", notice=problem))
    assert role is not None
    tiers = rank_tiers_for(guild.id)
    if any(tier["role_id"] == str(role.id) for tier in tiers):
        return redirect(url_for("ranks_page", notice="هذه الرتبة موجودة في المسار بالفعل."))
    tiers.append({
        "role_id": str(role.id),
        "name": role.name,
        "category": category,
        "points": required_points,
    })
    with DATA_LOCK:
        guild_data(guild.id)["rank_tiers"] = tiers
        save_data()
    schedule_all_rank_syncs(guild.id)
    return redirect(url_for("ranks_page", notice="تمت إضافة الرتبة وتفعيل الترقية التلقائية."))


@dashboard.post("/admin/ranks/update")
@guild_required
@permission_required("ranks")
def update_rank_tier() -> Any:
    guild = selected_guild()
    assert guild is not None
    role, category, required_points = requested_rank_values(guild)
    problem = rank_role_problem(guild, role)
    if problem:
        return redirect(url_for("ranks_page", notice=problem))
    assert role is not None
    tiers = rank_tiers_for(guild.id)
    tier = next((item for item in tiers if item["role_id"] == str(role.id)), None)
    if not tier:
        return redirect(url_for("ranks_page", notice="الرتبة غير موجودة في المسار."))
    tier.update({"name": role.name, "category": category, "points": required_points})
    with DATA_LOCK:
        guild_data(guild.id)["rank_tiers"] = tiers
        save_data()
    schedule_all_rank_syncs(guild.id)
    return redirect(url_for("ranks_page", notice="تم تحديث الرتبة وإعادة ترتيب المسار."))


@dashboard.post("/admin/ranks/delete")
@guild_required
@permission_required("ranks")
def delete_rank_tier() -> Any:
    guild = selected_guild()
    assert guild is not None
    role_id = dashboard_id(request.form.get("role_id", ""))
    with DATA_LOCK:
        guild_data(guild.id)["rank_tiers"] = [
            tier for tier in rank_tiers_for(guild.id) if tier["role_id"] != role_id
        ]
        save_data()
    removed_ids = {int(role_id)} if role_id and role_id.isdigit() else None
    schedule_all_rank_syncs(guild.id, removed_ids)
    return redirect(url_for("ranks_page", notice="تمت إزالة الرتبة من مسار الترقيات."))


@dashboard.post("/admin/points")
@guild_required
@permission_required("points")
def update_points() -> Any:
    guild = selected_guild()
    assert guild is not None
    member_id = dashboard_id(request.form.get("member_id", ""))
    try:
        amount = max(0, int(request.form.get("points", "0")))
    except ValueError:
        amount = 0
    if not member_id:
        return redirect(url_for("points_page", notice="Discord ID غير صحيح."))
    display_name = request.form.get("display_name", "").strip()
    member = guild.get_member(int(member_id)) if member_id.isdigit() else None
    set_points(member_id, amount, display_name or (member.display_name if member else ""), guild.id)
    schedule_rank_sync(guild.id, int(member_id))
    return redirect(url_for("points_page", notice="تم تحديث النقاط وفحص الترقية التلقائية."))


@dashboard.post("/admin/points/delete")
@guild_required
@permission_required("points")
def delete_points() -> Any:
    guild = selected_guild()
    assert guild is not None
    store = guild_data(guild.id)
    member_id = dashboard_id(request.form.get("member_id", ""))
    if member_id:
        with DATA_LOCK:
            store["points"].pop(member_id, None)
            store["members"].pop(member_id, None)
            save_data()
        schedule_rank_sync(guild.id, int(member_id))
    return redirect(url_for("points_page", notice="تم حذف السجل."))


def sync_from_dashboard() -> None:
    if not BOT_LOOP or not BOT_LOOP.is_running():
        return
    future = asyncio.run_coroutine_threadsafe(sync_commands(), BOT_LOOP)

    def log_sync_result(done: Any) -> None:
        try:
            done.result()
        except Exception:
            logger.exception("Slash command sync failed after dashboard change.")

    future.add_done_callback(log_sync_result)


@dashboard.post("/admin/shortcuts")
@guild_required
@permission_required("shortcuts")
def create_shortcut() -> Any:
    guild = selected_guild()
    assert guild is not None
    try:
        alias = clean_command_name(request.form.get("command_name", ""))
    except ValueError as error:
        return redirect(url_for("shortcuts_page", notice=str(error)))
    target = resolve_target(request.form.get("target_command", ""), guild.id)
    if not target or alias in BUILTIN_COMMANDS:
        return redirect(url_for("shortcuts_page", notice="الاختصار أو الأمر الهدف غير صالح."))
    with DATA_LOCK:
        guild_data(guild.id)["shortcuts"][alias] = target
        save_data()
    sync_from_dashboard()
    return redirect(url_for("shortcuts_page", notice=f"تم إنشاء /{alias}."))


@dashboard.post("/admin/shortcuts/delete")
@guild_required
@permission_required("shortcuts")
def delete_shortcut() -> Any:
    guild = selected_guild()
    assert guild is not None
    with DATA_LOCK:
        guild_data(guild.id)["shortcuts"].pop(request.form.get("command_name", "").strip().lower(), None)
        save_data()
    sync_from_dashboard()
    return redirect(url_for("shortcuts_page", notice="تم حذف الاختصار."))


@dashboard.post("/admin/settings")
@guild_required
@permission_required("settings")
def update_settings() -> Any:
    guild = selected_guild()
    assert guild is not None
    with DATA_LOCK:
        settings = guild_data(guild.id)["settings"]
        for key in ("dashboard_title", "server_name", "staff_role_name"):
            settings[key] = request.form.get(key, "").strip()[:80]
        accent = request.form.get("accent_color", "").strip()
        if re.fullmatch(r"#[0-9a-fA-F]{6}", accent):
            settings["accent_color"] = accent
        save_data()
    return redirect(url_for("settings_page", notice="تم حفظ إعدادات السيرفر."))


def run_dashboard() -> None:
    dashboard.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)


def main() -> None:
    threading.Thread(target=run_dashboard, name="dashboard", daemon=True).start()
    logger.info("Dashboard listening on 0.0.0.0:%s", PORT)
    bot.run(TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
