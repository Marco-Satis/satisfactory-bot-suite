#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
watchdog_bot.py - Satisfactory Server Watchdog Bot
"""

import os
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Any
from collections import deque

import discord
from discord import app_commands
from discord.ext import commands, tasks

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from common import (
    has_permission, log_admin, safe_subprocess, safe_systemctl,
    get_satisfactory_performance, apply_performance_tweaks,
    wait_for_server_ready, persistent_state, check_global_rate,
    shutdown_handler, OWNER_ID, ADMIN_LOG_CHANNEL_ID, SATISFACTORY_SERVICE,
    SERVER_DOWN_THRESHOLD, MEMORY_LEAK_THRESHOLD, CONTINUOUS_HIGH_CPU
)

TOKEN = os.getenv('TOKEN_WATCHDOG_BOT') or ""

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("watchdog-bot")

intents = discord.Intents.default()
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)

# -------- Watchdog Class --------
class ServerWatchdog:
    def __init__(self):
        self.down_counter = 0
        self.high_cpu_counter = 0
        self.high_memory_counter = 0
        self.restart_count = 0
        self.last_restart = None
        
        self.performance_history = deque(maxlen=20)
        self.alert_cooldowns = {}
        
        self.memory_warning_threshold = MEMORY_LEAK_THRESHOLD * 0.8
        self.cpu_warning_threshold = 85.0
        self.alert_cooldown_minutes = 10
        
        self.server_was_running = False
        self.crash_detection_enabled = True
        
        self.max_restarts_per_hour = 3
        self.restart_history = deque(maxlen=10)
        
        self._lock = asyncio.Lock()
        self.load_state()
    
    def load_state(self):
        try:
            state = persistent_state.get('watchdog_state', {})
            self.down_counter = state.get('down_counter', 0)
            self.high_cpu_counter = state.get('high_cpu_counter', 0)
            self.high_memory_counter = state.get('high_memory_counter', 0)
            self.restart_count = state.get('restart_count', 0)
            self.last_restart = state.get('last_restart')
            
            restart_hist = state.get('restart_history', [])
            if isinstance(restart_hist, list):
                self.restart_history = deque(restart_hist, maxlen=10)
            
            log.info(f"Watchdog-State geladen: {self.restart_count} Restarts")
        except Exception as e:
            log.error(f"Fehler beim Laden des Watchdog-States: {e}")
    
    async def save_state(self):
        async with self._lock:
            try:
                state = {
                    'down_counter': self.down_counter,
                    'high_cpu_counter': self.high_cpu_counter,
                    'high_memory_counter': self.high_memory_counter,
                    'restart_count': self.restart_count,
                    'last_restart': self.last_restart,
                    'restart_history': list(self.restart_history)
                }
                await persistent_state.set('watchdog_state', state)
            except Exception as e:
                log.error(f"Fehler beim Speichern des Watchdog-States: {e}")
    
    def can_restart(self) -> bool:
        now = datetime.now()
        hour_ago = now - timedelta(hours=1)
        
        recent_restarts = []
        for restart in self.restart_history:
            try:
                restart_time = datetime.fromisoformat(restart['timestamp'])
                if restart_time > hour_ago:
                    recent_restarts.append(restart)
            except (KeyError, ValueError):
                continue
        
        return len(recent_restarts) < self.max_restarts_per_hour
    
    def should_send_alert(self, alert_type: str) -> bool:
        if alert_type not in self.alert_cooldowns:
            return True
        
        last_alert = self.alert_cooldowns[alert_type]
        cooldown_end = last_alert + timedelta(minutes=self.alert_cooldown_minutes)
        return datetime.now() > cooldown_end
    
    def set_alert_cooldown(self, alert_type: str):
        self.alert_cooldowns[alert_type] = datetime.now()
    
    async def check_server_status(self) -> Dict[str, Any]:
        status = {
            'timestamp': datetime.now().isoformat(),
            'service_active': False,
            'process_running': False,
            'performance': None,
            'issues': [],
            'actions_taken': []
        }
        
        try:
            result = await safe_systemctl('is-active', SATISFACTORY_SERVICE)
            service_active = result and result.stdout.strip() == 'active'
            status['service_active'] = service_active
            
            perf = await get_satisfactory_performance()
            status['performance'] = perf
            status['process_running'] = bool(perf.get('pid'))
            
            if perf.get('pid'):
                self.performance_history.append({
                    'timestamp': datetime.now(),
                    'cpu': perf['cpu_percent'],
                    'memory': perf['memory_mb'],
                    'players': perf['estimated_players']
                })
            
            # Crash-Detection
            if self.server_was_running and not status['process_running']:
                if self.crash_detection_enabled:
                    status['issues'].append('server_crash')
                    log.warning("Server-Crash erkannt!")
            
            self.server_was_running = status['process_running']
            
        except Exception as e:
            log.error(f"Server-Status-Check fehlgeschlagen: {e}")
            status['error'] = str(e)
        
        return status
    
    async def handle_server_down(self) -> List[str]:
        actions = []
        
        try:
            self.down_counter += 1
            await self.save_state()
            
            log.warning(f"Server offline erkannt ({self.down_counter}/{SERVER_DOWN_THRESHOLD})")
            
            if self.should_send_alert('server_down'):
                await log_admin(
                    bot, 
                    f"⚠️ Server offline erkannt ({self.down_counter}/{SERVER_DOWN_THRESHOLD})",
                    ping_owner=(self.down_counter >= SERVER_DOWN_THRESHOLD)
                )
                self.set_alert_cooldown('server_down')
            
            if self.down_counter >= SERVER_DOWN_THRESHOLD:
                if self.can_restart():
                    restart_success = await self.restart_server("Watchdog: Server offline")
                    
                    if restart_success:
                        actions.append("restart_successful")
                        self.down_counter = 0
                        await self.save_state()
                    else:
                        actions.append("restart_failed")
                else:
                    await log_admin(
                        bot,
                        f"❌ Restart-Limit erreicht ({self.max_restarts_per_hour}/h)",
                        ping_owner=True
                    )
                    actions.append("restart_rate_limited")
        
        except Exception as e:
            log.error(f"Server-Down-Handler Fehler: {e}")
            actions.append(f"error: {e}")
        
        return actions
    
    async def handle_high_memory(self, memory_mb: int) -> List[str]:
        actions = []
        
        try:
            if memory_mb >= MEMORY_LEAK_THRESHOLD:
                await log_admin(
                    bot,
                    f"🚨 KRITISCHER SPEICHERVERBRAUCH: {memory_mb} MB ≥ {MEMORY_LEAK_THRESHOLD} MB",
                    ping_owner=True
                )
                
                if self.can_restart():
                    restart_success = await self.restart_server(f"Watchdog: Memory Leak ({memory_mb} MB)")
                    
                    if restart_success:
                        actions.append("restart_memory_leak")
                        self.high_cpu_counter = 0
                        self.high_memory_counter = 0
                        await self.save_state()
                    else:
                        actions.append("restart_failed_memory")
                else:
                    actions.append("restart_rate_limited_memory")
            
            elif memory_mb >= self.memory_warning_threshold:
                self.high_memory_counter += 1
                
                if self.should_send_alert('high_memory'):
                    await log_admin(
                        bot,
                        f"⚠️ Hoher Speicherverbrauch: {memory_mb} MB"
                    )
                    self.set_alert_cooldown('high_memory')
                    actions.append("memory_warning")
            else:
                if self.high_memory_counter > 0:
                    self.high_memory_counter = 0
                    await self.save_state()
                    actions.append("memory_normalized")
        
        except Exception as e:
            log.error(f"High-Memory-Handler Fehler: {e}")
            actions.append(f"error: {e}")
        
        return actions
    
    async def handle_high_cpu(self, cpu_percent: float, estimated_players: int) -> List[str]:
        actions = []
        
        try:
            if cpu_percent >= 95.0:
                self.high_cpu_counter += 1
                await self.save_state()
                
                if self.should_send_alert('high_cpu'):
                    await log_admin(
                        bot,
                        f"⚠️ Hohe CPU-Last: {cpu_percent:.1f}% ({self.high_cpu_counter}/{CONTINUOUS_HIGH_CPU})"
                    )
                    self.set_alert_cooldown('high_cpu')
                
                if self.high_cpu_counter >= CONTINUOUS_HIGH_CPU:
                    if estimated_players > 0:
                        await log_admin(
                            bot,
                            f"⚠️ Hohe CPU-Last mit {estimated_players} Spielern - Restart verzögert",
                            ping_owner=True
                        )
                        self.high_cpu_counter = max(CONTINUOUS_HIGH_CPU - 2, 0)
                        await self.save_state()
                        actions.append("cpu_restart_delayed_players")
                    else:
                        if self.can_restart():
                            restart_success = await self.restart_server(f"Watchdog: CPU Überlast ({cpu_percent:.1f}%)")
                            
                            if restart_success:
                                actions.append("restart_cpu_overload")
                                self.high_cpu_counter = 0
                                await self.save_state()
                            else:
                                actions.append("restart_failed_cpu")
                        else:
                            actions.append("restart_rate_limited_cpu")
            else:
                if self.high_cpu_counter > 0:
                    self.high_cpu_counter = 0
                    await self.save_state()
                    
                    if self.should_send_alert('cpu_normalized'):
                        await log_admin(bot, "✅ CPU-Last normalisiert")
                        self.set_alert_cooldown('cpu_normalized')
                    
                    actions.append("cpu_normalized")
        
        except Exception as e:
            log.error(f"High-CPU-Handler Fehler: {e}")
            actions.append(f"error: {e}")
        
        return actions
    
    async def restart_server(self, reason: str) -> bool:
        async with self._lock:
            try:
                await log_admin(bot, f"🔄 Server-Restart wird ausgelöst: {reason}")
                
                stop_result = await safe_systemctl('stop', SATISFACTORY_SERVICE)
                
                if not stop_result or stop_result.returncode != 0:
                    await log_admin(bot, "⚠️ Normaler Stop fehlgeschlagen, versuche Force-Stop...")
                    kill_result = await safe_subprocess(['sudo', 'pkill', '-f', 'FactoryServer'])
                    
                    if not kill_result or kill_result.returncode != 0:
                        await log_admin(bot, "❌ Force-Stop fehlgeschlagen", ping_owner=True)
                        return False
                
                await asyncio.sleep(15)
                
                start_result = await safe_systemctl('start', SATISFACTORY_SERVICE)
                
                if not start_result or start_result.returncode != 0:
                    error_msg = start_result.stderr if start_result else 'Unbekannt'
                    await log_admin(bot, f"❌ Server-Start fehlgeschlagen: {error_msg}", ping_owner=True)
                    return False
                
                self.restart_count += 1
                self.last_restart = datetime.now().isoformat()
                
                restart_entry = {
                    'timestamp': self.last_restart,
                    'reason': reason,
                    'restart_number': self.restart_count
                }
                self.restart_history.append(restart_entry)
                
                await self.save_state()
                
                await log_admin(bot, f"✅ Server-Restart erfolgreich (#{self.restart_count})")
                
                if await wait_for_server_ready():
                    perf = await get_satisfactory_performance()
                    if perf.get('pid'):
                        await apply_performance_tweaks(perf['pid'])
                    
                    await log_admin(bot, "✅ Server bereit nach Watchdog-Restart")
                else:
                    await log_admin(bot, "⚠️ Server gestartet aber Bereitschaft nicht bestätigt", ping_owner=True)
                
                return True
                
            except Exception as e:
                await log_admin(bot, f"❌ Server-Restart fehlgeschlagen: {e}", ping_owner=True)
                return False
    
    def get_status_summary(self) -> Dict[str, Any]:
        return {
            'down_counter': self.down_counter,
            'high_cpu_counter': self.high_cpu_counter,
            'high_memory_counter': self.high_memory_counter,
            'restart_count': self.restart_count,
            'last_restart': self.last_restart,
            'restart_history_count': len(self.restart_history),
            'performance_history_count': len(self.performance_history),
            'can_restart': self.can_restart(),
            'crash_detection_enabled': self.crash_detection_enabled
        }

watchdog = ServerWatchdog()

# -------- Hauptüberwachungsschleife --------
@tasks.loop(minutes=1)
async def patrol():
    try:
        status = await watchdog.check_server_status()
        
        if 'error' in status:
            log.error(f"Patrol-Fehler: {status['error']}")
            return
        
        actions_taken = []
        
        if not status['service_active'] or not status['process_running']:
            actions = await watchdog.handle_server_down()
            actions_taken.extend(actions)
        else:
            if watchdog.down_counter > 0:
                watchdog.down_counter = 0
                await watchdog.save_state()
                await log_admin(bot, "✅ Server wieder online")
            
            perf = status['performance']
            if perf and perf.get('pid'):
                memory_actions = await watchdog.handle_high_memory(perf['memory_mb'])
                actions_taken.extend(memory_actions)
                
                cpu_actions = await watchdog.handle_high_cpu(perf['cpu_percent'], perf['estimated_players'])
                actions_taken.extend(cpu_actions)
        
        if actions_taken:
            log.info(f"Watchdog-Aktionen: {', '.join(actions_taken)}")
        
    except Exception as e:
        log.error(f"Patrol-Hauptschleife Fehler: {e}")

@tasks.loop(hours=24)
async def daily_report():
    try:
        summary = watchdog.get_status_summary()
        
        report = f"""📊 **Täglicher Watchdog-Bericht**
        
**Zähler:**
- Neustarts heute: {summary['restart_count']}
- Down-Counter: {summary['down_counter']}/{SERVER_DOWN_THRESHOLD}
- CPU-Counter: {summary['high_cpu_counter']}/{CONTINUOUS_HIGH_CPU}"""
        
        if summary['last_restart']:
            try:
                last_restart = datetime.fromisoformat(summary['last_restart'])
                hours_ago = (datetime.now() - last_restart).total_seconds() / 3600
                report += f"\n\n**Letzter Restart:** vor {hours_ago:.1f}h"
            except:
                report += f"\n\n**Letzter Restart:** {summary['last_restart']}"
        
        await log_admin(bot, report)
        
        watchdog.restart_count = 0
        await watchdog.save_state()
        
    except Exception as e:
        log.error(f"Daily-Report Fehler: {e}")

# -------- Slash Commands --------
@bot.tree.command(name="watchdog_status", description="Zeigt Watchdog-Status")
@check_global_rate()
async def watchdog_status_cmd(interaction: discord.Interaction):
    if not has_permission(interaction.user):
        await interaction.response.send_message("🚫 Keine Berechtigung", ephemeral=True)
        return
    
    try:
        summary = watchdog.get_status_summary()
        
        embed = discord.Embed(
            title="👁️ Watchdog Status",
            color=discord.Color.blue(),
            timestamp=datetime.now()
        )
        
        embed.add_field(name="Down-Counter", value=f"{summary['down_counter']}/{SERVER_DOWN_THRESHOLD}", inline=True)
        embed.add_field(name="CPU-Counter", value=f"{summary['high_cpu_counter']}/{CONTINUOUS_HIGH_CPU}", inline=True)
        embed.add_field(name="Neustarts heute", value=str(summary['restart_count']), inline=True)
        
        restart_status = "✅ Erlaubt" if summary['can_restart'] else "❌ Rate-Limit"
        embed.add_field(name="Restart-Status", value=restart_status, inline=True)
        
        crash_status = "✅ Aktiv" if summary['crash_detection_enabled'] else "❌ Deaktiviert"
        embed.add_field(name="Crash Detection", value=crash_status, inline=True)
        
        try:
            perf = await get_satisfactory_performance()
            if perf['pid']:
                embed.add_field(
                    name="Server-Performance",
                    value=f"CPU: {perf['cpu_percent']:.1f}% | RAM: {perf['memory_mb']} MB | Spieler: {perf['estimated_players']}",
                    inline=False
                )
            else:
                embed.add_field(name="Server-Status", value="🔴 Offline", inline=False)
        except Exception:
            embed.add_field(name="Performance", value="Fehler beim Abrufen", inline=False)
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
        
    except Exception as e:
        await interaction.response.send_message(f"❌ Status-Abruf fehlgeschlagen: {e}", ephemeral=True)

@bot.tree.command(name="force_restart", description="Erzwingt Server-Restart")
@check_global_rate()
async def force_restart_cmd(interaction: discord.Interaction):
    if not has_permission(interaction.user):
        await interaction.response.send_message("🚫 Keine Berechtigung", ephemeral=True)
        return
    
    await interaction.response.defer(ephemeral=True)
    
    try:
        await log_admin(bot, f"🚨 FORCE-RESTART ausgelöst von {interaction.user}")
        
        restart_success = await watchdog.restart_server(f"Force-Restart von {interaction.user}")
        
        if restart_success:
            await interaction.followup.send("✅ Force-Restart erfolgreich durchgeführt", ephemeral=True)
        else:
            await interaction.followup.send("❌ Force-Restart fehlgeschlagen", ephemeral=True)
        
    except Exception as e:
        await interaction.followup.send(f"❌ Force-Restart Fehler: {e}", ephemeral=True)

# -------- Bot Events --------
@bot.event
async def on_ready():
    await bot.change_presence(activity=discord.Game(name="Server Watchdog"))
    
    try:
        await bot.tree.sync()
        log.info("Slash-Commands synchronisiert")
    except Exception as e:
        log.error(f"Command-Sync Fehler: {e}")
    
    if not patrol.is_running():
        patrol.start()
    
    if not daily_report.is_running():
        daily_report.start()
    
    log.info(f"✅ Watchdog-Bot bereit als {bot.user}")
    await log_admin(bot, "👁️ **Watchdog-Bot online** - Überwachung aktiv")
    
    summary = watchdog.get_status_summary()
    await log_admin(
        bot, 
        f"Status: Restarts: {summary['restart_count']}, Down: {summary['down_counter']}, CPU: {summary['high_cpu_counter']}"
    )

async def cleanup():
    try:
        log.info("Watchdog-Bot Cleanup...")
        await bot.close()
    except Exception as e:
        log.error(f"Cleanup Fehler: {e}")

shutdown_handler.add_cleanup_task(cleanup)

if __name__ == "__main__":
    if not TOKEN:
        log.error("❌ Discord-Token fehlt! Setze TOKEN_WATCHDOG_BOT in .env")
        exit(1)
    
    try:
        bot.run(TOKEN)
    except Exception as e:
        log.error(f"❌ Bot-Start fehlgeschlagen: {e}")
        exit(1)