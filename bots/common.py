#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
common.py - Zentrale Funktionen und Konstanten für die Satisfactory Bot Suite
Vollständige Version mit allen Sicherheitsfunktionen
"""

import os
import re
import json
import glob
import pickle
import asyncio
import logging
import subprocess
import shlex
import shutil
import signal
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, Any, Set, List, Union, Callable
from collections import deque
from functools import wraps

import psutil
import discord
from discord.ext import commands
from discord import app_commands

try:
    from cryptography.fernet import Fernet
    CRYPTO_AVAILABLE = True
except ImportError:
    CRYPTO_AVAILABLE = False

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# -------- Logging Setup --------
def sanitize_log_data(data: str) -> str:
    """Entfernt sensitive Daten aus Log-Nachrichten"""
    if not isinstance(data, str):
        data = str(data)
    
    patterns = [
        (r'token["\s]*[:=]["\s]*[a-zA-Z0-9._-]+', '[TOKEN_REDACTED]'),
        (r'password["\s]*[:=]["\s]*\S+', '[PASSWORD_REDACTED]'),
        (r'secret["\s]*[:=]["\s]*\S+', '[SECRET_REDACTED]'),
        (r'key["\s]*[:=]["\s]*[a-zA-Z0-9._-]+', '[KEY_REDACTED]'),
        (r'Bearer\s+[a-zA-Z0-9._-]+', '[BEARER_REDACTED]'),
        (r'Authorization:\s*[^\s]+', '[AUTH_REDACTED]'),
    ]
    
    for pattern, replacement in patterns:
        data = re.sub(pattern, replacement, data, flags=re.IGNORECASE)
    
    return data

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
log = logging.getLogger("satisfactory-common")

# -------- IDs / Rollen / Berechtigungen --------
OWNER_ID = int(os.getenv('OWNER_ID', '0'))
GAME_ADMIN_ROLE_ID = int(os.getenv('GAME_ADMIN_ROLE_ID', '0'))

def _parse_ids(varname: str) -> Set[int]:
    """Konvertiert kommaseparierte ID-Listen zu int-Set"""
    raw = os.getenv(varname, '').strip()
    if not raw:
        return set()
    out = set()
    for part in raw.split(','):
        part = part.strip()
        if part.isdigit():
            out.add(int(part))
    return out

SATISFACTORY_ROLE_IDS = _parse_ids('SATISFACTORY_ROLE_IDS')
ALLOWED_USER_IDS = _parse_ids('ALLOWED_USER_IDS')
PUBLIC_COMMANDS = set(s.strip() for s in os.getenv('PUBLIC_COMMANDS', '').split(',') if s.strip())

# -------- Kanal-IDs --------
PUBLIC_STATUS_CHANNEL_ID = int(os.getenv('PUBLIC_STATUS_CHANNEL_ID', '0'))
ADMIN_LOG_CHANNEL_ID = int(os.getenv('ADMIN_LOG_CHANNEL_ID', '0'))
CHAT_BRIDGE_CHANNEL_ID = int(os.getenv('CHAT_BRIDGE_CHANNEL_ID', '0'))

# -------- Server Konfiguration --------
SATISFACTORY_SERVICE = os.getenv('SATISFACTORY_SERVICE', 'satisfactory.service')
SATISFACTORY_CONFIG_PATH = os.getenv('SATISFACTORY_CONFIG_PATH', 
    '/home/satisfactory/.config/Epic/FactoryGame/Saved/Config/LinuxServer')
SATISFACTORY_SAVEGAME_PATH = os.getenv('SATISFACTORY_SAVEGAME_PATH',
    '/home/satisfactory/.config/Epic/FactoryGame/Saved/SaveGames/server')
BLUEPRINT_PATH = os.getenv('BLUEPRINT_PATH', 
    '/home/satisfactory/.config/Epic/FactoryGame/Saved/SaveGames/blueprints/1.1')
BACKUP_PATH = os.getenv('BACKUP_PATH', 
    str(Path(SATISFACTORY_SAVEGAME_PATH) / "Backups"))

# -------- Grenzwerte --------
SERVER_DOWN_THRESHOLD = int(os.getenv('SERVER_DOWN_THRESHOLD', '2'))
MEMORY_LEAK_THRESHOLD = int(os.getenv('MEMORY_LEAK_THRESHOLD', '12000'))
CONTINUOUS_HIGH_CPU = int(os.getenv('CONTINUOUS_HIGH_CPU', '5'))
MAX_BLUEPRINT_SIZE = 10 * 1024 * 1024  # 10MB
MAX_BACKUPS = int(os.getenv('MAX_BACKUPS', '10'))
MAX_LOCAL_BACKUPS = int(os.getenv('MAX_LOCAL_BACKUPS', '20'))

# -------- RCON Konfiguration --------
RCON_ENABLED = os.getenv('RCON_ENABLED', 'false').lower() == 'true'
RCON_HOST = os.getenv('RCON_HOST', '127.0.0.1')
RCON_PORT = int(os.getenv('RCON_PORT', '15777'))
RCON_PASSWORD = os.getenv('RCON_PASSWORD', '')

# -------- Sicherheits-Whitelist --------
ALLOWED_COMMANDS = {
    'systemctl': ['start', 'stop', 'restart', 'status', 'is-active', 'is-failed'],
    'sysctl': ['-n', '-w'],
    'renice': ['-n', '-p'],
    'ionice': ['-c1', '-c2', '-c3', '-n0', '-n1', '-n2', '-n3', '-n4', '-n5', '-n6', '-n7', '-p'],
    'taskset': ['-cp'],
    'pkill': ['-f'],
    'pgrep': ['-f'],
    'fail2ban-client': ['status', 'set', 'unbanall'],
    'chown': [],  # Wird separat validiert
}

ALLOWED_SYSCTL_PARAMS = {
    'net.core.rmem_default', 'net.core.wmem_default', 'net.core.rmem_max', 'net.core.wmem_max',
    'net.core.netdev_max_backlog', 'net.core.somaxconn', 'net.ipv4.tcp_rmem', 'net.ipv4.tcp_wmem',
    'net.ipv4.tcp_congestion_control', 'net.ipv4.tcp_notsent_lowat', 'net.ipv4.tcp_slow_start_after_idle',
    'vm.swappiness', 'vm.vfs_cache_pressure', 'vm.dirty_ratio', 'vm.dirty_background_ratio',
    'vm.nr_hugepages'
}

def validate_command(command: str, args: List[str]) -> bool:
    """Validiert ob Command und Argumente erlaubt sind"""
    if command not in ALLOWED_COMMANDS:
        log.warning(f"Command not in whitelist: {command}")
        return False
    
    allowed_args = ALLOWED_COMMANDS[command]
    
    # Spezielle Validierung für verschiedene Commands
    if command == 'systemctl':
        if len(args) < 2:
            return False
        action, service = args[0], args[1]
        return action in allowed_args and service.endswith('.service')
    
    elif command == 'sysctl':
        if len(args) < 2:
            return False
        if args[0] == '-w' and len(args) >= 2:
            param = args[1].split('=')[0]
            return param in ALLOWED_SYSCTL_PARAMS
        elif args[0] == '-n' and len(args) >= 2:
            return args[1] in ALLOWED_SYSCTL_PARAMS
    
    elif command == 'chown':
        # Nur satisfactory:satisfactory erlaubt
        if len(args) >= 2 and args[0] == 'satisfactory:satisfactory':
            return True
        return False
    
    # Standard-Validierung für andere Commands
    for arg in args:
        if not (arg in allowed_args or 
                arg.replace('-', '').replace('_', '').replace('.', '').replace('/', '').isalnum()):
            return False
    
    return True

async def safe_subprocess(cmd: List[str], timeout: int = 30) -> Optional[subprocess.CompletedProcess]:
    """Führt subprocess sicher aus"""
    try:
        if not cmd or not isinstance(cmd, list):
            return None
        
        command = cmd[0]
        args = cmd[1:]
        
        if not validate_command(command, args):
            log.error(f"Command validation failed: {command} {args}")
            return None
        
        # Sichere Quote-Behandlung
        safe_cmd = [shlex.quote(str(arg)) if ' ' in str(arg) else str(arg) for arg in cmd]
        
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, 
            lambda: subprocess.run(safe_cmd, capture_output=True, text=True, timeout=timeout, check=False)
        )
        
        return result
        
    except subprocess.TimeoutExpired:
        log.error(f"Command timeout: {cmd}")
        return None
    except Exception as e:
        log.error(f"Subprocess error: {sanitize_log_data(str(e))}")
        return None

async def safe_systemctl(action: str, service: str) -> Optional[subprocess.CompletedProcess]:
    """Sichere systemctl-Wrapper-Funktion"""
    return await safe_subprocess(['systemctl', action, service])

# -------- Berechtigungs-System --------
def has_permission(user: Union[discord.User, discord.Member]) -> bool:
    """Prüft umfassende Berechtigungen"""
    if not user:
        return False
    
    uid = getattr(user, 'id', 0)
    
    # Owner hat immer Berechtigung
    if uid == OWNER_ID:
        return True
    
    # Explizit erlaubte User
    if uid in ALLOWED_USER_IDS:
        return True
    
    # Rolle-basierte Berechtigung
    if isinstance(user, discord.Member):
        role_ids = {r.id for r in user.roles}
        
        # Game Admin Rolle
        if GAME_ADMIN_ROLE_ID and GAME_ADMIN_ROLE_ID in role_ids:
            return True
        
        # Satisfactory-spezifische Rollen
        if SATISFACTORY_ROLE_IDS and (role_ids & SATISFACTORY_ROLE_IDS):
            return True
    
    return False

def is_public_command(command_name: str) -> bool:
    """Prüft ob Command öffentlich zugänglich ist"""
    return command_name.lower() in PUBLIC_COMMANDS

# -------- Sicherheits-Utilities --------
def safe_filename(filename: str) -> Optional[str]:
    """Validiert und bereinigt Dateinamen"""
    if not filename:
        return None
    
    # Nur Basename verwenden (Path Traversal verhindern)
    base = os.path.basename(filename)
    
    # Gefährliche Zeichen entfernen
    safe = re.sub(r'[<>:"|?*\\]', '', base)
    
    # Nur alphanumerische Zeichen, Punkte, Unterstriche, Bindestriche
    if not re.match(r'^[a-zA-Z0-9._-]+$', safe):
        return None
    
    # Länge begrenzen
    if len(safe) > 100:
        return None
    
    return safe

# -------- Performance-Monitoring --------
async def get_satisfactory_performance() -> Dict[str, Any]:
    """Holt Server-Performance-Daten async"""
    data = {
        'pid': None, 
        'cpu_percent': 0.0, 
        'memory_mb': 0, 
        'estimated_players': 0,
        'status': 'unknown',
        'uptime': None
    }
    
    try:
        # Service Status prüfen
        result = await safe_systemctl('is-active', SATISFACTORY_SERVICE)
        if result:
            data['status'] = result.stdout.strip()
        
        # Prozess finden
        await asyncio.sleep(0)  # Yield control
        
        for proc in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_info', 'connections', 'create_time']):
            try:
                name = proc.info['name'] or ''
                if any(server_name in name for server_name in ['FactoryServer', 'UE4Server', 'UnrealServer']):
                    data['pid'] = proc.info['pid']
                    
                    # CPU mit kleinem Interval
                    data['cpu_percent'] = proc.cpu_percent(interval=0.1)
                    
                    # Memory in MB
                    if proc.info['memory_info']:
                        data['memory_mb'] = int(proc.info['memory_info'].rss / 1024 / 1024)
                    
                    # Uptime
                    if proc.info['create_time']:
                        uptime = datetime.now() - datetime.fromtimestamp(proc.info['create_time'])
                        data['uptime'] = uptime.total_seconds()
                    
                    # Spieler schätzen über TCP-Verbindungen
                    try:
                        connections = proc.connections(kind='tcp')
                        established = len([c for c in connections if c.status == 'ESTABLISHED'])
                        data['estimated_players'] = max(0, established - 5)
                    except (psutil.AccessDenied, psutil.NoSuchProcess):
                        pass
                    
                    break
                    
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
                
    except Exception as e:
        log.error(f"Performance-Monitor Fehler: {sanitize_log_data(str(e))}")
    
    return data

async def apply_performance_tweaks(pid: Optional[int] = None) -> bool:
    """Wendet Performance-Optimierungen auf Prozess an"""
    try:
        if not pid:
            perf = await get_satisfactory_performance()
            pid = perf.get('pid')
        
        if not pid:
            log.warning("Kein Server-Prozess für Performance-Tweaks gefunden")
            return False
        
        # CPU-Priorität erhöhen
        result1 = await safe_subprocess(['renice', '-n', '-5', '-p', str(pid)])
        
        # I/O-Priorität setzen
        result2 = await safe_subprocess(['ionice', '-c2', '-n0', '-p', str(pid)])
        
        # CPU-Affinity auf alle Kerne
        try:
            cpu_count = psutil.cpu_count() or 4
            cpu_list = ','.join(str(i) for i in range(cpu_count))
            result3 = await safe_subprocess(['taskset', '-cp', cpu_list, str(pid)])
        except Exception:
            result3 = None
        
        success = bool(result1 and result1.returncode == 0)
        log.info(f"Performance-Tweaks für PID {pid}: {'✅' if success else '❌'}")
        return success
        
    except Exception as e:
        log.error(f"Performance-Tweak Fehler: {sanitize_log_data(str(e))}")
        return False

# -------- Server-Management --------
async def wait_for_server_ready(service: str = SATISFACTORY_SERVICE, max_wait: int = 120) -> bool:
    """Wartet bis Server vollständig bereit ist"""
    start_time = datetime.now()
    
    log.info(f"Warte auf Server-Bereitschaft ({max_wait}s)")
    
    while (datetime.now() - start_time).seconds < max_wait:
        # Service-Status prüfen
        result = await safe_systemctl('is-active', service)
        if not result or result.stdout.strip() != 'active':
            await asyncio.sleep(3)
            continue
        
        # Prozess-Status prüfen
        perf = await get_satisfactory_performance()
        if perf['pid'] and perf['cpu_percent'] > 0:
            # Kurz stabilisieren lassen
            await asyncio.sleep(5)
            log.info("✅ Server ist bereit")
            return True
        
        await asyncio.sleep(3)
    
    log.warning(f"❌ Server nicht bereit nach {max_wait}s")
    return False

# -------- Discord Utilities --------
async def log_admin(bot: commands.Bot, message: str, ping_owner: bool = False):
    """Sendet Nachricht an Admin-Log-Channel"""
    if not ADMIN_LOG_CHANNEL_ID:
        return
    
    channel = bot.get_channel(ADMIN_LOG_CHANNEL_ID)
    if not channel:
        return
    
    try:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        text = f"[{timestamp}] {sanitize_log_data(message)}"
        
        if ping_owner and OWNER_ID:
            text = f"<@{OWNER_ID}> **KRITISCH**\n{text}"
        
        # Discord Nachrichtenlimit beachten
        if len(text) > 2000:
            text = text[:1950] + "... [GEKÜRZT]"
        
        await channel.send(text)
    except Exception as e:
        log.error(f"Admin-Log Fehler: {sanitize_log_data(str(e))}")

async def log_public(bot: commands.Bot, message: str):
    """Sendet Nachricht an öffentlichen Status-Channel"""
    if not PUBLIC_STATUS_CHANNEL_ID:
        return
    
    channel = bot.get_channel(PUBLIC_STATUS_CHANNEL_ID)
    if not channel:
        return
    
    try:
        sanitized = sanitize_log_data(message)
        if len(sanitized) > 2000:
            sanitized = sanitized[:1950] + "... [GEKÜRZT]"
        await channel.send(sanitized)
    except Exception as e:
        log.error(f"Public-Log Fehler: {sanitize_log_data(str(e))}")

# -------- Rate Limiting --------
class GlobalRateLimiter:
    """Globaler Rate Limiter für Commands"""
    
    def __init__(self, max_calls: int = 3, per_seconds: int = 10):
        self.max_calls = max_calls
        self.per = timedelta(seconds=per_seconds)
        self.store: Dict[int, deque] = {}
        self._lock = asyncio.Lock()
    
    async def allow(self, user_id: int) -> bool:
        """Prüft ob User Rate-Limit einhalten kann"""
        async with self._lock:
            now = datetime.now()
            
            if user_id not in self.store:
                self.store[user_id] = deque()
            
            user_calls = self.store[user_id]
            
            # Alte Einträge entfernen
            while user_calls and now - user_calls[0] > self.per:
                user_calls.popleft()
            
            # Limit prüfen
            if len(user_calls) >= self.max_calls:
                return False
            
            # Neuen Call registrieren
            user_calls.append(now)
            return True

# Globaler Rate Limiter
global_rate_limiter = GlobalRateLimiter(3, 10)

def check_global_rate():
    """Decorator für Rate Limiting"""
    async def predicate(interaction: discord.Interaction) -> bool:
        if not await global_rate_limiter.allow(interaction.user.id):
            raise app_commands.CheckFailure("⏱️ Rate-Limit aktiv. Bitte kurz warten.")
        return True
    return app_commands.check(predicate)

# -------- Persistenter State --------
STATE_FILE = Path(__file__).parent / 'bot_state.pickle'

class PersistentState:
    """Thread-safe persistenter State für Bot-Daten"""
    
    def __init__(self, filepath: Path = STATE_FILE):
        self.filepath = filepath
        self.data = self._load()
        self._lock = asyncio.Lock()
        self._encryption_key = self._get_or_create_key()
    
    def _get_or_create_key(self) -> Optional[bytes]:
        """Holt oder erstellt Verschlüsselungskey"""
        if not CRYPTO_AVAILABLE:
            return None
        
        key_file = self.filepath.parent / '.state_key'
        if key_file.exists():
            try:
                return key_file.read_bytes()
            except Exception:
                pass
        
        # Neuen Key erstellen
        key = Fernet.generate_key()
        try:
            key_file.write_bytes(key)
            key_file.chmod(0o600)
        except Exception:
            pass
        return key
    
    def _load(self) -> Dict[str, Any]:
        """Lädt gespeicherten State oder erstellt neuen"""
        if self.filepath.exists():
            try:
                with open(self.filepath, 'rb') as f:
                    data = pickle.load(f)
                return data if isinstance(data, dict) else {}
            except Exception as e:
                log.warning(f"State-Datei konnte nicht geladen werden: {e}")
        return {}
    
    async def save(self):
        """Speichert aktuellen State (async)"""
        async with self._lock:
            try:
                # Backup der alten Datei
                if self.filepath.exists():
                    backup = self.filepath.with_suffix('.backup')
                    self.filepath.rename(backup)
                
                with open(self.filepath, 'wb') as f:
                    pickle.dump(self.data, f)
                
                # Berechtigungen setzen
                self.filepath.chmod(0o600)
                
                # Backup löschen bei erfolgreichem Speichern
                backup = self.filepath.with_suffix('.backup')
                if backup.exists():
                    backup.unlink()
                    
            except Exception as e:
                log.error(f"State-Speichern fehlgeschlagen: {sanitize_log_data(str(e))}")
    
    def get(self, key: str, default: Any = None) -> Any:
        """Holt Wert aus State"""
        return self.data.get(key, default)
    
    async def set(self, key: str, value: Any):
        """Setzt Wert und speichert async"""
        self.data[key] = value
        await self.save()

# Globale State-Instanz
persistent_state = PersistentState()

# -------- RCON Client --------
try:
    from rcon.source import Client as RCONClient
    RCON_AVAILABLE = True
except ImportError:
    RCONClient = None
    RCON_AVAILABLE = False

async def rcon_execute(command: str) -> Optional[str]:
    """Führt RCON-Command aus"""
    if not RCON_AVAILABLE or not RCON_ENABLED or not RCON_PASSWORD:
        return None
    
    try:
        loop = asyncio.get_event_loop()
        
        def _rcon_call():
            with RCONClient(RCON_HOST, RCON_PORT, passwd=RCON_PASSWORD, timeout=10) as client:
                return client.run(command)
        
        result = await loop.run_in_executor(None, _rcon_call)
        return result
        
    except Exception as e:
        log.error(f"RCON-Fehler: {sanitize_log_data(str(e))}")
        return None

# -------- Graceful Shutdown --------
class ShutdownHandler:
    """Behandelt graceful shutdown"""
    
    def __init__(self):
        self.cleanup_tasks: List[Callable] = []
        self._shutdown_event = asyncio.Event()
        
        # Signal-Handler registrieren
        for sig in [signal.SIGINT, signal.SIGTERM]:
            signal.signal(sig, self._signal_handler)
    
    def _signal_handler(self, signum, frame):
        """Signal-Handler"""
        log.info(f"Signal {signum} empfangen, starte graceful shutdown...")
        asyncio.create_task(self.shutdown())
    
    async def shutdown(self):
        """Führt graceful shutdown durch"""
        if self._shutdown_event.is_set():
            return
        
        self._shutdown_event.set()
        
        log.info("Führe Cleanup-Tasks aus...")
        for task in self.cleanup_tasks:
            try:
                if asyncio.iscoroutinefunction(task):
                    await task()
                else:
                    task()
            except Exception as e:
                log.error(f"Cleanup-Task Fehler: {e}")
        
        # State speichern
        await persistent_state.save()
        
        log.info("Graceful shutdown abgeschlossen")
    
    def add_cleanup_task(self, task: Callable):
        """Fügt Cleanup-Task hinzu"""
        self.cleanup_tasks.append(task)

# Globaler Shutdown-Handler
shutdown_handler = ShutdownHandler()