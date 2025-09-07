#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
manager_bot.py - Satisfactory Server Manager Bot
Finale sichere Version mit rclone Backups
"""

import os
import asyncio
import logging
import tarfile
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from common import (
    has_permission, log_admin, log_public, safe_filename, safe_subprocess,
    get_satisfactory_performance, apply_performance_tweaks, 
    wait_for_server_ready, check_global_rate, persistent_state,
    OWNER_ID, ADMIN_LOG_CHANNEL_ID, PUBLIC_STATUS_CHANNEL_ID,
    SATISFACTORY_SERVICE, BLUEPRINT_PATH, BACKUP_PATH,
    SATISFACTORY_SAVEGAME_PATH, MAX_BLUEPRINT_SIZE, shutdown_handler
)

# Bot Setup
TOKEN = os.getenv('TOKEN_SERVER_BOT') or ""

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("manager-bot")

intents = discord.Intents.default()
intents.guilds = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# -------- Backup Manager --------
class BackupManager:
    """Verwaltet rclone-basierte Backups"""
    
    def __init__(self):
        self.rclone_remote = os.getenv('RCLONE_REMOTE', '')
        self.rclone_path = os.getenv('RCLONE_BACKUP_PATH', '/satisfactory-backups')
        self.local_save_dir = os.getenv('LOCAL_SAVE_DIR', SATISFACTORY_SAVEGAME_PATH)
        self.enabled = bool(self.rclone_remote)
        
    async def create_backup(self, name: Optional[str] = None) -> Optional[Path]:
        """Erstellt lokales Backup"""
        try:
            if not name:
                name = f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            
            backup_dir = Path(BACKUP_PATH)
            backup_dir.mkdir(parents=True, exist_ok=True)
            
            backup_file = backup_dir / f"{name}.tar.gz"
            savegame_path = Path(self.local_save_dir)
            
            if not savegame_path.exists():
                log.error(f"SaveGame-Pfad nicht gefunden: {savegame_path}")
                return None
            
            with tarfile.open(backup_file, "w:gz") as tar:
                tar.add(savegame_path, arcname="SaveGames")
            
            log.info(f"Backup erstellt: {backup_file.name}")
            return backup_file
            
        except Exception as e:
            log.error(f"Backup-Erstellung fehlgeschlagen: {e}")
            return None
    
    async def upload_backup(self, backup_file: Path) -> bool:
        """Lädt Backup via rclone hoch"""
        if not self.enabled:
            return False
        
        try:
            cmd = [
                'rclone', 'copy', str(backup_file),
                f"{self.rclone_remote}:{self.rclone_path}",
                '--progress'
            ]
            
            result = await safe_subprocess(cmd, timeout=600)
            success = result and result.returncode == 0
            
            if success:
                log.info(f"rclone Upload erfolgreich: {backup_file.name}")
            else:
                log.error(f"rclone Upload fehlgeschlagen: {result.stderr if result else 'Unknown'}")
            
            return success
            
        except Exception as e:
            log.error(f"rclone Upload Fehler: {e}")
            return False

backup_manager = BackupManager()

# -------- Blueprint Manager --------
class BlueprintManager:
    """Verwaltet Satisfactory Blueprints sicher"""
    
    def __init__(self):
        self.blueprint_path = Path(BLUEPRINT_PATH)
        self.blueprint_path.mkdir(parents=True, exist_ok=True)
    
    def list_blueprints(self) -> List[str]:
        """Listet alle Blueprints auf"""
        try:
            blueprints = set()
            for bp_file in self.blueprint_path.glob("*.sbp"):
                blueprints.add(bp_file.stem)
            return sorted(blueprints)
        except Exception as e:
            log.error(f"Blueprint-Listing fehlgeschlagen: {e}")
            return []
    
    async def delete_blueprint(self, name: str) -> bool:
        """Löscht Blueprint und zugehörige Dateien"""
        try:
            safe_name = safe_filename(name)
            if not safe_name:
                return False
            
            deleted = False
            for ext in ['.sbp', '.sbpcfg']:
                file_path = self.blueprint_path / f"{safe_name}{ext}"
                if file_path.exists():
                    file_path.unlink()
                    deleted = True
                    log.info(f"Blueprint-Datei gelöscht: {file_path.name}")
            
            return deleted
        except Exception as e:
            log.error(f"Blueprint-Löschung fehlgeschlagen: {e}")
            return False
    
    async def validate_upload(self, attachment: discord.Attachment) -> Optional[str]:
        """Validiert Blueprint-Upload"""
        try:
            if attachment.size > MAX_BLUEPRINT_SIZE:
                return f"Datei zu groß: {attachment.size / 1024 / 1024:.1f} MB"
            
            safe_name = safe_filename(attachment.filename)
            if not safe_name:
                return "Ungültiger Dateiname"
            
            if not safe_name.lower().endswith(('.sbp', '.sbpcfg')):
                return "Nur .sbp und .sbpcfg Dateien erlaubt"
            
            return None
        except Exception as e:
            return f"Validierung fehlgeschlagen: {e}"
    
    async def save_blueprint(self, attachment: discord.Attachment) -> bool:
        """Speichert Blueprint-Datei"""
        try:
            safe_name = safe_filename(attachment.filename)
            if not safe_name:
                return False
            
            file_path = self.blueprint_path / safe_name
            data = await attachment.read()
            
            with open(file_path, 'wb') as f:
                f.write(data)
            
            log.info(f"Blueprint gespeichert: {safe_name}")
            return True
        except Exception as e:
            log.error(f"Blueprint-Speichern fehlgeschlagen: {e}")
            return False

blueprint_manager = BlueprintManager()

# -------- Announcement Modal --------
class AnnouncementModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="Server-Ankündigung")
        
        self.message = discord.ui.TextInput(
            label="Nachricht",
            placeholder="Server-Restart in 10 Minuten...",
            max_length=300,
            style=discord.TextStyle.paragraph
        )
        self.add_item(self.message)
    
    async def on_submit(self, interaction: discord.Interaction):
        message_text = self.message.value.strip()
        
        if not message_text:
            await interaction.response.send_message("❌ Leere Nachricht", ephemeral=True)
            return
        
        try:
            from common import rcon_execute
            rcon_result = await rcon_execute(f"Broadcast {message_text}")
            
            if rcon_result is not None:
                await interaction.response.send_message(
                    f"📢 Ankündigung gesendet:\n```{message_text}```", 
                    ephemeral=True
                )
                await log_admin(bot, f"📢 Ankündigung von {interaction.user}: {message_text}")
            else:
                await interaction.response.send_message(
                    f"⚠️ RCON nicht verfügbar - nur Discord-Log:\n```{message_text}```", 
                    ephemeral=True
                )
                await log_public(bot, f"📢 **Server-Ankündigung**: {message_text}")
                
        except Exception as e:
            await interaction.response.send_message(f"❌ Fehler: {e}", ephemeral=True)

# -------- Slash Commands --------
@bot.tree.command(name="status", description="Zeigt Server-Status und Performance")
@check_global_rate()
async def status_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    
    try:
        result = await safe_subprocess(['systemctl', 'is-active', SATISFACTORY_SERVICE])
        status = result.stdout.strip() if result else 'unknown'
        
        perf = await get_satisfactory_performance()
        
        color = discord.Color.green() if status == 'active' else discord.Color.red()
        embed = discord.Embed(
            title="🎮 Satisfactory Server Status",
            color=color,
            timestamp=datetime.now()
        )
        
        status_icon = "🟢" if status == 'active' else "🔴"
        embed.add_field(
            name="Status",
            value=f"{status_icon} {'Online' if status == 'active' else 'Offline'}",
            inline=True
        )
        
        if perf['pid']:
            embed.add_field(name="CPU", value=f"{perf['cpu_percent']:.1f}%", inline=True)
            embed.add_field(name="RAM", value=f"{perf['memory_mb']} MB", inline=True)
            embed.add_field(name="Spieler", value=str(perf['estimated_players']), inline=True)
            
            if perf['uptime']:
                uptime_hours = perf['uptime'] / 3600
                embed.add_field(name="Uptime", value=f"{uptime_hours:.1f}h", inline=True)
        
        await interaction.followup.send(embed=embed, ephemeral=True)
        
    except Exception as e:
        await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)

@bot.tree.command(name="backup", description="Erstellt Server-Backup")
@check_global_rate()
async def backup_cmd(interaction: discord.Interaction):
    if not has_permission(interaction.user):
        await interaction.response.send_message("🚫 Keine Berechtigung", ephemeral=True)
        return
    
    await interaction.response.defer(ephemeral=True)
    
    try:
        backup_file = await backup_manager.create_backup()
        
        if not backup_file:
            await interaction.followup.send("❌ Backup-Erstellung fehlgeschlagen", ephemeral=True)
            return
        
        upload_success = await backup_manager.upload_backup(backup_file)
        
        if upload_success:
            await interaction.followup.send(
                f"✅ Backup erfolgreich:\n📁 Lokal: `{backup_file.name}`\n☁️ rclone: Hochgeladen",
                ephemeral=True
            )
            await log_admin(bot, f"✅ Backup erstellt von {interaction.user}: {backup_file.name}")
        else:
            await interaction.followup.send(
                f"⚠️ Backup lokal erstellt, rclone-Upload fehlgeschlagen:\n📁 Lokal: `{backup_file.name}`",
                ephemeral=True
            )
        
    except Exception as e:
        await interaction.followup.send(f"❌ Backup-Fehler: {e}", ephemeral=True)

@bot.tree.command(name="announce", description="Sendet Ankündigung an Server")
@check_global_rate()
async def announce_cmd(interaction: discord.Interaction):
    if not has_permission(interaction.user):
        await interaction.response.send_message("🚫 Keine Berechtigung", ephemeral=True)
        return
    
    modal = AnnouncementModal()
    await interaction.response.send_modal(modal)

@bot.tree.command(name="restart", description="Startet Server neu")
@check_global_rate()
async def restart_cmd(interaction: discord.Interaction):
    if not has_permission(interaction.user):
        await interaction.response.send_message("🚫 Keine Berechtigung", ephemeral=True)
        return
    
    await interaction.response.defer(ephemeral=True)
    
    try:
        await log_admin(bot, f"🔄 Server-Restart initiiert von {interaction.user}")
        await log_public(bot, "🔄 **Server wird neu gestartet** - Bitte kurz warten...")
        
        stop_result = await safe_subprocess(['sudo', 'systemctl', 'stop', SATISFACTORY_SERVICE], timeout=60)
        
        if not stop_result or stop_result.returncode != 0:
            await interaction.followup.send("❌ Server-Stop fehlgeschlagen", ephemeral=True)
            return
        
        await asyncio.sleep(10)
        
        start_result = await safe_subprocess(['sudo', 'systemctl', 'start', SATISFACTORY_SERVICE], timeout=30)
        
        if not start_result or start_result.returncode != 0:
            await interaction.followup.send("❌ Server-Start fehlgeschlagen", ephemeral=True)
            await log_admin(bot, "❌ Server-Start fehlgeschlagen!", ping_owner=True)
            return
        
        await interaction.followup.send("🔄 Server wird neu gestartet...", ephemeral=True)
        
        if await wait_for_server_ready():
            await apply_performance_tweaks()
            await log_public(bot, "✅ **Server-Restart abgeschlossen** - Server ist wieder online!")
            await log_admin(bot, f"✅ Server-Restart erfolgreich (von {interaction.user})")
        else:
            await log_admin(bot, "⚠️ Server gestartet, aber Bereitschaft unbestätigt", ping_owner=True)
        
    except Exception as e:
        await interaction.followup.send(f"❌ Restart-Fehler: {e}", ephemeral=True)
        await log_admin(bot, f"❌ Server-Restart Fehler: {e}", ping_owner=True)

@bot.tree.command(name="list_blueprints", description="Zeigt verfügbare Blueprints")
@check_global_rate()
async def list_blueprints_cmd(interaction: discord.Interaction):
    if not has_permission(interaction.user):
        await interaction.response.send_message("🚫 Keine Berechtigung", ephemeral=True)
        return
    
    try:
        blueprints = blueprint_manager.list_blueprints()
        
        if not blueprints:
            await interaction.response.send_message("📋 Keine Blueprints gefunden", ephemeral=True)
            return
        
        embed = discord.Embed(
            title="📋 Verfügbare Blueprints",
            color=discord.Color.blue(),
            timestamp=datetime.now()
        )
        
        chunk_size = 20
        chunks = [blueprints[i:i + chunk_size] for i in range(0, len(blueprints), chunk_size)]
        
        for i, chunk in enumerate(chunks):
            chunk_text = "\n".join(f"• {bp}" for bp in chunk)
            embed.add_field(
                name=f"Blueprints {i*chunk_size + 1}-{min((i+1)*chunk_size, len(blueprints))}",
                value=chunk_text,
                inline=False
            )
        
        embed.set_footer(text=f"Gesamt: {len(blueprints)} Blueprints")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        
    except Exception as e:
        await interaction.response.send_message(f"❌ Blueprint-Liste Fehler: {e}", ephemeral=True)

@bot.tree.command(name="delete_blueprint", description="Löscht Blueprint")
@check_global_rate()
async def delete_blueprint_cmd(interaction: discord.Interaction, name: str):
    if not has_permission(interaction.user):
        await interaction.response.send_message("🚫 Keine Berechtigung", ephemeral=True)
        return
    
    try:
        success = await blueprint_manager.delete_blueprint(name)
        
        if success:
            await interaction.response.send_message(f"✅ Blueprint gelöscht: {name}", ephemeral=True)
            await log_admin(bot, f"🗑️ Blueprint gelöscht von {interaction.user}: {name}")
        else:
            await interaction.response.send_message(f"❌ Blueprint nicht gefunden: {name}", ephemeral=True)
        
    except Exception as e:
        await interaction.response.send_message(f"❌ Löschfehler: {e}", ephemeral=True)

# -------- Message Handler für Blueprint-Upload --------
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return
    
    if (len(message.attachments) >= 1 and 
        any(att.filename.lower().endswith(('.sbp', '.sbpcfg')) for att in message.attachments) and
        has_permission(message.author)):
        
        try:
            uploaded_files = []
            errors = []
            
            for attachment in message.attachments:
                if attachment.filename.lower().endswith(('.sbp', '.sbpcfg')):
                    error = await blueprint_manager.validate_upload(attachment)
                    if error:
                        errors.append(f"{attachment.filename}: {error}")
                        continue
                    
                    success = await blueprint_manager.save_blueprint(attachment)
                    if success:
                        uploaded_files.append(attachment.filename)
                    else:
                        errors.append(f"{attachment.filename}: Speichern fehlgeschlagen")
            
            if uploaded_files:
                await message.add_reaction("✅")
                await log_admin(bot, f"📎 Blueprints hochgeladen von {message.author}: {', '.join(uploaded_files)}")
            
            if errors:
                error_text = "\n".join(errors)
                await message.reply(f"⚠️ **Blueprint-Upload Fehler:**\n```{error_text}```", delete_after=30)
        
        except Exception as e:
            await message.add_reaction("❌")
            log.error(f"Blueprint-Upload Fehler: {e}")
    
    await bot.process_commands(message)

# -------- Background Tasks --------
@tasks.loop(minutes=5)
async def status_update():
    try:
        perf = await get_satisfactory_performance()
        await persistent_state.set('last_performance', perf)
    except Exception as e:
        log.error(f"Status-Update Fehler: {e}")

# -------- Bot Events --------
@bot.event
async def on_ready():
    await bot.change_presence(activity=discord.Game(name="Satisfactory Manager"))
    
    try:
        await bot.tree.sync()
        log.info("Slash-Commands synchronisiert")
    except Exception as e:
        log.error(f"Command-Sync Fehler: {e}")
    
    if not status_update.is_running():
        status_update.start()
    
    log.info(f"✅ Manager-Bot bereit als {bot.user}")
    await log_admin(bot, "🎮 **Manager-Bot online** - Alle Systeme bereit")

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        try:
            await interaction.response.send_message(str(error), ephemeral=True)
        except:
            try:
                await interaction.followup.send(str(error), ephemeral=True)
            except:
                pass
    else:
        log.error(f"Command-Fehler: {error}")

async def cleanup():
    try:
        log.info("Manager-Bot Cleanup...")
        await bot.close()
    except Exception as e:
        log.error(f"Cleanup Fehler: {e}")

shutdown_handler.add_cleanup_task(cleanup)

if __name__ == "__main__":
    if not TOKEN:
        log.error("❌ Discord-Token fehlt! Setze TOKEN_SERVER_BOT in .env")
        exit(1)
    
    try:
        bot.run(TOKEN)
    except Exception as e:
        log.error(f"❌ Bot-Start fehlgeschlagen: {e}")
        exit(1)