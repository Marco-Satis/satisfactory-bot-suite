#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
optimize_bot.py - Satisfactory Server Optimierungsbot
"""

import os
import asyncio
import logging
from datetime import datetime
from typing import Dict, Any

import discord
from discord import app_commands
from discord.ext import commands, tasks

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from common import (
    has_permission, log_admin, safe_subprocess, 
    get_satisfactory_performance, apply_performance_tweaks,
    persistent_state, check_global_rate, shutdown_handler,
    OWNER_ID, ADMIN_LOG_CHANNEL_ID, SATISFACTORY_SERVICE
)

TOKEN = os.getenv('TOKEN_OPTIMIZE_BOT') or ""

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("optimize-bot")

intents = discord.Intents.default()
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)

# -------- System Optimizer --------
class SystemOptimizer:
    def __init__(self):
        self.optimization_history = []
        self.max_history = 50
        
        # Sichere sysctl-Parameter
        self.safe_sysctl_params = {
            'net.core.rmem_default': '262144',
            'net.core.wmem_default': '262144', 
            'net.core.rmem_max': '16777216',
            'net.core.wmem_max': '16777216',
            'net.core.netdev_max_backlog': '2500',
            'net.core.somaxconn': '1024',
            'net.ipv4.tcp_rmem': '4096 87380 16777216',
            'net.ipv4.tcp_wmem': '4096 65536 16777216',
            'net.ipv4.tcp_congestion_control': 'bbr',
            'vm.swappiness': '10',
            'vm.vfs_cache_pressure': '50',
        }
    
    async def apply_network_optimizations(self) -> Dict[str, Any]:
        result = {
            'success': True,
            'applied': [],
            'failed': [],
            'timestamp': datetime.now().isoformat()
        }
        
        try:
            for param, value in self.safe_sysctl_params.items():
                try:
                    cmd_result = await safe_subprocess(['sudo', 'sysctl', '-w', f'{param}={value}'])
                    
                    if cmd_result and cmd_result.returncode == 0:
                        result['applied'].append(f"{param}={value}")
                        log.info(f"✓ {param} = {value}")
                    else:
                        result['failed'].append(f"{param}={value}")
                        log.warning(f"✗ Fehlgeschlagen: {param}")
                        
                except Exception as e:
                    result['failed'].append(f"{param}: {e}")
            
            result['success'] = len(result['applied']) > len(result['failed'])
            
        except Exception as e:
            result['success'] = False
            result['error'] = str(e)
        
        return result
    
    async def optimize_process_priority(self, pid: int = None) -> Dict[str, Any]:
        result = {
            'success': False,
            'pid': pid,
            'nice_applied': False,
            'ionice_applied': False,
            'timestamp': datetime.now().isoformat()
        }
        
        try:
            if not pid:
                perf = await get_satisfactory_performance()
                pid = perf.get('pid')
                result['pid'] = pid
            
            if not pid:
                result['error'] = "Server-Prozess nicht gefunden"
                return result
            
            # Nice-Wert setzen
            nice_result = await safe_subprocess(['sudo', 'renice', '-n', '-10', '-p', str(pid)])
            if nice_result and nice_result.returncode == 0:
                result['nice_applied'] = True
            
            # I/O-Priorität setzen
            ionice_result = await safe_subprocess(['sudo', 'ionice', '-c2', '-n0', '-p', str(pid)])
            if ionice_result and ionice_result.returncode == 0:
                result['ionice_applied'] = True
            
            result['success'] = result['nice_applied'] or result['ionice_applied']
            
        except Exception as e:
            result['error'] = str(e)
        
        return result
    
    async def perform_full_optimization(self, reason: str = "Manual") -> Dict[str, Any]:
        optimization = {
            'timestamp': datetime.now().isoformat(),
            'reason': reason,
            'network_optimization': None,
            'process_optimization': None,
            'overall_success': False
        }
        
        try:
            log.info(f"Starte Optimierung: {reason}")
            
            # Netzwerk-Optimierungen
            optimization['network_optimization'] = await self.apply_network_optimizations()
            
            # Prozess-Optimierungen
            perf = await get_satisfactory_performance()
            if perf.get('pid'):
                optimization['process_optimization'] = await self.optimize_process_priority(perf['pid'])
            else:
                optimization['process_optimization'] = {'success': False, 'reason': 'Server offline'}
            
            # Gesamterfolg
            successes = [
                optimization['network_optimization']['success'],
                optimization['process_optimization']['success']
            ]
            optimization['overall_success'] = any(successes)
            
            # Historie aktualisieren
            self.optimization_history.append(optimization)
            if len(self.optimization_history) > self.max_history:
                self.optimization_history.pop(0)
            
            await persistent_state.set('optimization_history', self.optimization_history)
            
        except Exception as e:
            optimization['error'] = str(e)
        
        return optimization
    
    def get_optimization_stats(self) -> Dict[str, Any]:
        if not self.optimization_history:
            return {'total': 0, 'successful': 0, 'failed': 0, 'success_rate': 0}
        
        total = len(self.optimization_history)
        successful = sum(1 for opt in self.optimization_history if opt.get('overall_success', False))
        failed = total - successful
        success_rate = (successful / total) * 100 if total > 0 else 0
        
        return {
            'total': total,
            'successful': successful,
            'failed': failed,
            'success_rate': success_rate,
            'last_optimization': self.optimization_history[-1]['timestamp'] if self.optimization_history else None
        }

optimizer = SystemOptimizer()

# -------- Slash Commands --------
@bot.tree.command(name="optimize_now", description="Startet sofortige Server-Optimierung")
@check_global_rate()
async def optimize_now_cmd(interaction: discord.Interaction):
    if not has_permission(interaction.user):
        await interaction.response.send_message("🚫 Keine Berechtigung", ephemeral=True)
        return
    
    await interaction.response.defer(ephemeral=True)
    
    try:
        await log_admin(bot, f"🔧 Optimierung gestartet von {interaction.user}")
        
        result = await optimizer.perform_full_optimization(f"Manual by {interaction.user}")
        
        embed = discord.Embed(
            title="⚙️ Optimierungsergebnis",
            color=discord.Color.green() if result['overall_success'] else discord.Color.orange(),
            timestamp=datetime.now()
        )
        
        network_status = "✅" if result['network_optimization']['success'] else "❌"
        process_status = "✅" if result['process_optimization']['success'] else "❌"
        
        embed.add_field(
            name="Netzwerk",
            value=f"{network_status} {len(result['network_optimization']['applied'])} Parameter",
            inline=True
        )
        
        embed.add_field(
            name="Prozess",
            value=f"{process_status} {'PID ' + str(result['process_optimization']['pid']) if result['process_optimization']['pid'] else 'Offline'}",
            inline=True
        )
        
        await interaction.followup.send(embed=embed, ephemeral=True)
        
        if result['overall_success']:
            await log_admin(bot, "✅ Optimierung erfolgreich abgeschlossen")
        else:
            await log_admin(bot, "⚠️ Optimierung mit Problemen abgeschlossen")
        
    except Exception as e:
        await interaction.followup.send(f"❌ Optimierung fehlgeschlagen: {e}", ephemeral=True)

@bot.tree.command(name="optimization_status", description="Zeigt Optimierungsstatus")
@check_global_rate()
async def optimization_status_cmd(interaction: discord.Interaction):
    if not has_permission(interaction.user):
        await interaction.response.send_message("🚫 Keine Berechtigung", ephemeral=True)
        return
    
    try:
        stats = optimizer.get_optimization_stats()
        
        embed = discord.Embed(
            title="⚙️ Optimierungsstatus",
            color=discord.Color.blue(),
            timestamp=datetime.now()
        )
        
        embed.add_field(name="Gesamt", value=f"{stats['total']} Optimierungen", inline=True)
        embed.add_field(name="Erfolgreich", value=f"{stats['successful']} ({stats['success_rate']:.1f}%)", inline=True)
        embed.add_field(name="Fehlgeschlagen", value=f"{stats['failed']}", inline=True)
        
        if stats['last_optimization']:
            try:
                last_time = datetime.fromisoformat(stats['last_optimization'])
                time_diff = datetime.now() - last_time
                hours_ago = time_diff.total_seconds() / 3600
                embed.add_field(name="Letzte Optimierung", value=f"Vor {hours_ago:.1f} Stunden", inline=True)
            except:
                embed.add_field(name="Letzte Optimierung", value="Unbekannt", inline=True)
        
        perf = await get_satisfactory_performance()
        if perf['pid']:
            embed.add_field(
                name="Server-Performance",
                value=f"CPU: {perf['cpu_percent']:.1f}% | RAM: {perf['memory_mb']} MB",
                inline=False
            )
        else:
            embed.add_field(name="Server-Status", value="🔴 Offline", inline=False)
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
        
    except Exception as e:
        await interaction.response.send_message(f"❌ Status-Abruf fehlgeschlagen: {e}", ephemeral=True)

# -------- Background Tasks --------
@tasks.loop(hours=6)
async def periodic_optimization():
    try:
        await log_admin(bot, "🔄 Periodische Optimierung startet...")
        result = await optimizer.perform_full_optimization("Periodisch (6h)")
        
        if result['overall_success']:
            await log_admin(bot, "✅ Periodische Optimierung erfolgreich")
        else:
            await log_admin(bot, "⚠️ Periodische Optimierung mit Problemen")
    except Exception as e:
        await log_admin(bot, f"❌ Periodische Optimierung fehlgeschlagen: {e}")

# -------- Bot Events --------
@bot.event
async def on_ready():
    await bot.change_presence(activity=discord.Game(name="Server Optimizer"))
    
    try:
        await bot.tree.sync()
        log.info("Slash-Commands synchronisiert")
    except Exception as e:
        log.error(f"Command-Sync Fehler: {e}")
    
    if not periodic_optimization.is_running():
        periodic_optimization.start()
    
    try:
        stored_history = await persistent_state.get('optimization_history', [])
        if isinstance(stored_history, list):
            optimizer.optimization_history = stored_history[-optimizer.max_history:]
    except Exception as e:
        log.warning(f"Konnte Optimierungshistorie nicht laden: {e}")
    
    log.info(f"✅ Optimize-Bot bereit als {bot.user}")
    await log_admin(bot, "⚙️ **Optimize-Bot online** - Bereit für Systemoptimierungen")

async def cleanup():
    try:
        log.info("Optimize-Bot Cleanup...")
        await bot.close()
    except Exception as e:
        log.error(f"Cleanup Fehler: {e}")

shutdown_handler.add_cleanup_task(cleanup)

if __name__ == "__main__":
    if not TOKEN:
        log.error("❌ Discord-Token fehlt! Setze TOKEN_OPTIMIZE_BOT in .env")
        exit(1)
    
    try:
        bot.run(TOKEN)
    except Exception as e:
        log.error(f"❌ Bot-Start fehlgeschlagen: {e}")
        exit(1)