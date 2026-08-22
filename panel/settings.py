#!/usr/bin/env python3
"""Panneau de reglages d'un serveur Space Engineers dedie (macOS + Wine).

Sert une page locale qui lit et ecrit les fichiers de configuration du monde,
puis redemarre le serveur.

La page elle-meme ne charge aucune ressource distante : polices systeme
uniquement, aucun webfont, aucun CDN. Par defaut le serveur HTTP n'ecoute que
la boucle locale, et une ecoute hors boucle locale doit etre demandee
explicitement (SE_PANEL_ALLOW_REMOTE), sinon le panneau refuse de demarrer.

Le panneau n'a AUCUNE authentification et POST /api/appliquer reecrit les
fichiers du monde puis lance stop.sh et start.sh : quiconque atteint le port
peut le faire. Ce qui est verifie, c'est seulement que la requete vient bien
de cette machine (en-tetes Host et Origin, Content-Type sur le POST) ; cela
ferme le detournement par un navigateur, pas l'acces direct au port.

L'arret force (--force, qui court-circuite le garde-fou d'age de sauvegarde de
stop.sh) n'est JAMAIS implicite : il faut le demander depuis l'interface.

Configuration. Les memes valeurs que les scripts, lues dans le meme ordre :
config.sh (ou config.example.sh a defaut) est relu ici, et l'environnement
l'emporte sur le fichier.

  SE_ROOT                racine de l'installation   (defaut : dossier parent de ce fichier)
  SE_PREFIX              prefixe Wine               (defaut : $SE_ROOT/prefix)
  SE_WINE_USER           utilisateur Wine           (defaut : utilisateur courant)
  SE_WORLD               nom du monde               (defaut : MyWorld)
  SE_PANEL_HOST          interface d'ecoute         (defaut : 127.0.0.1)
  SE_PANEL_PORT          port d'ecoute              (defaut : 8777)
  SE_PANEL_ALLOW_REMOTE  autorise une ecoute hors boucle locale (defaut : 0)

Pour y acceder depuis une autre machine, prefere un tunnel SSH plutot que
d'ouvrir le port :  ssh -N -L 8777:127.0.0.1:8777 utilisateur@le-mac

Usage :  python3 panel/settings.py
"""
import datetime
import getpass
import html
import http.server
import ipaddress
import base64
import hashlib
import json
import os
import pathlib
import re
import socketserver
import shutil
import subprocess
import sys
import time

# --------------------------------------------------------------------------
# Localisation. Aucun chemin en dur : le script se situe lui-meme, lit le meme
# fichier de configuration que les scripts, et l'environnement l'emporte.
#
# Cet ordre n'est pas cosmetique : un outil qui vise un autre monde que stop.sh
# est un garde-fou de sauvegarde qui protege silencieusement le mauvais fichier
# (voir l'en-tete de scripts/common.sh).
# --------------------------------------------------------------------------


def _racine():
    """Racine de l'installation, resolue comme dans scripts/common.sh.

    SE_ROOT s'il est pose dans l'environnement, sinon le dossier parent de ce
    fichier. Aucun autre nom n'est accepte : un alias que les scripts shell
    ignorent ferait diverger le panneau du reste du depot.
    """
    val = os.environ.get("SE_ROOT")
    if val:
        return pathlib.Path(val).expanduser().resolve()
    return pathlib.Path(__file__).resolve().parent.parent


ROOT = _racine()

# Variables de config.sh qui concernent le panneau. Les autres (session tmux,
# delais de demarrage) ne servent qu'aux scripts shell.
_LUES = ("SE_PREFIX", "SE_WINE_USER", "SE_WORLD",
         "SE_PANEL_HOST", "SE_PANEL_PORT", "SE_PANEL_ALLOW_REMOTE")


def _config_shell(racine):
    """Valeurs de config.sh, lues comme les scripts les lisent.

    config.sh est un fichier shell : ses valeurs sont posees en
    `: "${VAR:=...}"`, ce sont donc des variables de shell et non des variables
    d'environnement, et elles peuvent contenir `$SE_ROOT` ou `$(id -un)`. Les
    relire a la main donnerait un autre resultat que scripts/common.sh, alors
    on sous-traite a bash exactement comme lui, puis on releve les valeurs
    obtenues.

    Renvoie un dictionnaire, vide si aucun fichier n'est lisible.
    """
    fichier = racine / "config.sh"
    if not fichier.is_file():
        fichier = racine / "config.example.sh"
    if not fichier.is_file():
        return {}
    script = ('. "$1" >/dev/null 2>&1 || exit 0\n'
              'for v in "${@:2}"; do printf "%s=%s\\0" "$v" "${!v-}"; done')
    env = dict(os.environ)
    env["SE_ROOT"] = str(racine)  # comme common.sh : la racine est resolue avant
    try:
        r = subprocess.run(["bash", "-c", script, "_", str(fichier), *_LUES],
                           capture_output=True, text=True, timeout=15, env=env)
    except (OSError, subprocess.SubprocessError):
        return {}
    out = {}
    for champ in r.stdout.split("\0"):
        cle, sep, val = champ.partition("=")
        if sep and val:
            out[cle] = val
    return out


CONF = _config_shell(ROOT)


def _reglage(nom, defaut=""):
    """Environnement d'abord, puis config.sh, puis le defaut interne."""
    return os.environ.get(nom) or CONF.get(nom) or defaut


def _entier(val, defaut):
    try:
        return int(str(val).strip())
    except (TypeError, ValueError):
        return defaut


PREFIX = pathlib.Path(_reglage("SE_PREFIX") or ROOT / "prefix").expanduser()
WINE_USER = _reglage("SE_WINE_USER") or getpass.getuser()
MONDE = _reglage("SE_WORLD") or "MyWorld"
HOTE = _reglage("SE_PANEL_HOST") or "127.0.0.1"
PORT = _entier(_reglage("SE_PANEL_PORT"), 8777)
AUTORISE_DISTANT = _reglage("SE_PANEL_ALLOW_REMOTE", "0").strip().lower() in ("1", "true", "yes", "on")


def _boucle_locale(hote):
    """Vrai si l'adresse d'ecoute ne sort pas de la machine."""
    if hote in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(hote).is_loopback
    except ValueError:
        # Nom d'hote quelconque : on ne peut pas garantir la boucle locale.
        return False


# --------------------------------------------------------------------------
# Garde d'acces.
#
# Le panneau n'a aucune authentification : son seul rempart est de n'ecouter
# que la boucle locale. Trois verifications ferment ce qui reste atteignable
# depuis un navigateur, sur cette machine comme ailleurs :
#
#   - Host : un nom DNS qui pointe sur 127.0.0.1 (DNS rebinding) permet a une
#     page distante de parler au panneau. On n'accepte que les noms de boucle
#     locale et, le cas echeant, l'adresse d'ecoute demandee explicitement.
#   - Origin : s'il est present, la requete a ete declenchee par une page. On
#     ne l'accepte que si cette page est le panneau lui-meme.
#   - Content-Type sur le POST : un formulaire HTML ne sait emettre que
#     urlencoded, multipart ou text/plain. Exiger application/json impose un
#     pre-vol CORS, qu'une page tierce ne passera pas.
#
# Cela ne remplace pas une authentification : qui atteint le port en direct
# (curl, script local) garde tout pouvoir. C'est pourquoi l'ecoute reste sur
# la boucle locale par defaut.
# --------------------------------------------------------------------------
HOTES_OK = {"127.0.0.1", "localhost", "::1"}
if HOTE:
    HOTES_OK.add(HOTE.strip().lower())

# Panneau volontairement ouvert sur un reseau : la liste blanche de noms n'a
# plus de sens (on ignore par quel nom ou quelle IP la machine sera jointe) et
# le rebinding n'apporte rien a qui atteint deja le port. On n'exige alors que
# la coherence Origin/Host, qui reste exacte dans tous les cas.
HOTE_LIBRE = AUTORISE_DISTANT and not _boucle_locale(HOTE)


def _autorite(brut):
    """Decoupe "hote[:port]" en (nom minuscule, port). (None, None) si invalide."""
    if not brut:
        return None, None
    h = brut.strip()
    if h.startswith("["):                       # IPv6 litteral : [::1]:8777
        fin = h.find("]")
        if fin < 0:
            return None, None
        nom, reste = h[1:fin], h[fin + 1:]
        port = reste[1:] if reste.startswith(":") else ""
    elif h.count(":") > 1:                      # IPv6 nu : en-tete Host invalide
        return None, None
    else:
        nom, _, port = h.partition(":")
    nom = nom.strip().lower()
    return (nom or None), port.strip()


def _hote_ok(brut):
    """Vrai si l'en-tete Host designe ce panneau."""
    nom, port = _autorite(brut)
    if nom is None:
        return False                            # absent (HTTP/1.0) ou illisible
    if port and port != str(PORT):
        return False
    return True if HOTE_LIBRE else nom in HOTES_OK


def _origine_ok(origine, hote):
    """Vrai si l'Origin designe exactement le Host de la meme requete.

    Un Origin absent est accepte : c'est le cas d'une navigation normale et
    des clients qui n'en emettent pas. Present, il doit etre identique, ce qui
    ecarte toute page tierce sans dependre de l'adresse d'ecoute.
    """
    o = (origine or "").strip()
    if not o:
        return True
    for prefixe in ("http://", "https://"):
        if o.lower().startswith(prefixe):
            return _autorite(o[len(prefixe):]) == _autorite(hote)
    return False                                # "null" et le reste : refuse


def _instance():
    """Dossier AppData du serveur dedie dans le prefixe Wine.

    L'utilisateur Wine ne porte pas toujours le meme nom que le compte macOS
    (et $USER est vide sous cron/launchd) : on tente le nom attendu, puis on
    resout par glob.
    """
    direct = PREFIX / "drive_c/users" / WINE_USER / "AppData/Roaming/SpaceEngineersDedicated"
    if direct.is_dir():
        return direct
    for essai in sorted(PREFIX.glob("drive_c/users/*/AppData/Roaming/SpaceEngineersDedicated")):
        if essai.is_dir():
            return essai
    return direct


INST = _instance()
MONDE_DIR = INST / "Saves" / MONDE
FICHIERS = [
    MONDE_DIR / "Sandbox_config.sbc",
    MONDE_DIR / "Sandbox.sbc",
    INST / "SpaceEngineers-Dedicated.cfg",
]
LECTURE = FICHIERS[0]

# Delais accordes aux deux scripts, en secondes.
#
# L'arret demande d'abord au jeu de s'arreter proprement, ce qu'il honore en
# deux secondes une fois le monde charge, mais qu'il met en file d'attente
# tant qu'il charge : stop.sh patiente jusqu'a SE_STOP_TIMEOUT (300 s par
# defaut). Le demarrage reessaie SE_START_ATTEMPTS fois, chacune jusqu'a
# SE_START_TIMEOUT. Couper avant la fin ferait declarer un echec au panneau
# sur un serveur qui finit par se lever, et laisserait les deux desaccordes.
DELAI_ARRET = 520
DELAI_DEMARRAGE = 3000

# --------------------------------------------------------------------------
# Schema des reglages : cle XML, libelle, type, aide, [choix]
# --------------------------------------------------------------------------
REGLAGES = [
 ("Rendement", [
  ("InventorySizeMultiplier", "Taille des inventaires", "num", "Multiplie la capacite du perso et des conteneurs. 1 = vanilla, 10 = confortable."),
  ("BlocksInventorySizeMultiplier", "Inventaire des blocs", "num", "Capacite des conteneurs et des blocs, independamment de celle du personnage."),
  ("AssemblerSpeedMultiplier", "Vitesse assembleurs", "num", "Rapidite de fabrication."),
  ("AssemblerEfficiencyMultiplier", "Rendement assembleurs", "num", "Moins de minerai par composant."),
  ("RefinerySpeedMultiplier", "Vitesse raffineries", "num", "Rapidite de raffinage."),
  ("WelderSpeedMultiplier", "Vitesse soudeuses", "num", "Rapidite de construction."),
  ("GrinderSpeedMultiplier", "Vitesse meuleuses", "num", "Rapidite de demontage."),
  ("HackSpeedMultiplier", "Vitesse de piratage", "num", "Rapidite pour prendre le controle d'un bloc ennemi."),
  ("HarvestRatioMultiplier", "Rendement du forage", "num", "Part du minerai reellement recuperee. Sous 1, la roche rend moins que ce qu'elle contient."),
  ("CharacterSpeedMultiplier", "Vitesse du personnage", "num", ""),
  ("EnvironmentDamageMultiplier", "Degats de l'environnement", "num", "Chutes, collisions, brulures."),
  ("SpawnShipTimeMultiplier", "Delai des vaisseaux de secours", "num", "0 = disponible immediatement."),
 ]),
 ("Survie", [
  ("FoodConsumptionRate", "Vitesse de la faim", "num", "Rythme auquel la barre de nourriture descend."),
  ("EnableSurvivalBuffs", "Barre de nourriture", "bool", "Desactive, plus de faim ni de soif."),
  ("AutoHealing", "Regeneration auto", "bool", "Le personnage se soigne seul dans un espace pressurise."),
  ("EnableOxygen", "Oxygene", "bool", ""),
  ("EnableOxygenPressurization", "Pressurisation", "bool", "Les pieces etanches se remplissent d'air. Sans ca, l'oxygene ne sert qu'aux reservoirs."),
  ("EnableJetpack", "Jetpack", "bool", ""),
  ("EnableRadiation", "Radiations", "bool", ""),
  ("EnableRespawnShips", "Vaisseaux de secours", "bool", "Reapparition avec une petite nacelle."),
  ("PermanentDeath", "Mort definitive", "bool", "A LA MORT, LE PERSONNAGE ET SES BLOCS SONT PERDUS."),
  ("EnableAutorespawn", "Reapparition automatique", "bool", ""),
  ("EnableReducedStatsOnRespawn", "Statistiques reduites a la reapparition", "bool", "On revient affame et assoiffe."),
  ("EnableSpaceSuitRespawn", "Reapparition en scaphandre", "bool", "Reapparaitre en combinaison plutot que dans un lit medical."),
  ("StartInRespawnScreen", "Demarrer sur l'ecran de reapparition", "bool", ""),
  ("SpawnWithTools", "Reapparaitre avec les outils", "bool", ""),
  ("RespawnShipDelete", "Supprimer l'ancien vaisseau de secours", "bool", "A la reapparition, le precedent est efface."),
  ("BackpackDespawnTimer", "Disparition du sac (min)", "num", "Duree avant que le sac laisse a la mort ne s'efface."),
 ]),
 ("Interface", [
  ("Enable3rdPersonView", "Vue a la troisieme personne", "bool", ""),
  ("ShowPlayerNamesOnHud", "Noms des joueurs sur l'ATH", "bool", ""),
  ("EnemyTargetIndicatorDistance", "Distance d'indicateur d'ennemi (m)", "num", ""),
  ("MaxHudChatMessageCount", "Messages de chat affiches", "num", ""),
  ("EnableGoodBotHints", "Conseils du bot d'aide", "bool", ""),
  ("EnableGamepadAimAssist", "Aide a la visee manette", "bool", ""),
  ("OffensiveWordsFiltering", "Filtre des mots offensants", "bool", ""),
  ("EnableFactionPlayerNames", "Nom de faction devant les pseudos", "bool", ""),
  ("RealisticSound", "Son realiste", "bool", "Pas de son dans le vide."),
  ("EnableSpectator", "Mode spectateur", "bool", "Camera libre. Donne une vision complete de la carte a qui l'active."),
 ]),
 ("Monde", [
  ("GameMode", "Mode de jeu", "choix", "Creatif donne les ressources infinies et la construction instantanee a tout le monde.", ["Survival", "Creative"]),
  ("MaxPlayers", "Joueurs maximum", "num", ""),
  ("AutoSaveInMinutes", "Sauvegarde auto (min)", "num", "Ce qui n'est pas sauvegarde est perdu a l'arret du serveur."),
  ("EnableSaving", "Sauvegarde activee", "bool", "A LAISSER ACTIF. Desactive, rien n'est jamais ecrit sur le disque."),
  ("EnvironmentHostility", "Hostilite", "choix", "Meteorites. CATACLYSM et ARMAGEDDON rasent les bases.", ["SAFE", "NORMAL", "CATACLYSM", "ARMAGEDDON"]),
  ("ExperimentalMode", "Mode experimental", "bool", "Requis par les scripts in-game et plusieurs reglages ci-dessous."),
  ("DestructibleBlocks", "Blocs destructibles", "bool", ""),
  ("EnableConvertToStation", "Conversion en station", "bool", "Transformer un vaisseau en station, qui ne bouge plus."),
  ("EnableSupergridding", "Supergridding", "bool", "Permet d'imbriquer petite et grande grille. Exploit connu, casse l'equilibre."),
  ("EnableRemoteBlockRemoval", "Retrait de bloc a distance", "bool", ""),
  ("EnableIngameScripts", "Scripts in-game", "bool", "Bloc programmable. Force le mode Experimental."),
  ("EnableScripterRole", "Role scripteur", "bool", "Restreint le bloc programmable aux joueurs promus Scripter."),
  ("EnableResearch", "Recherche", "bool", "Les blocs se debloquent en construisant, au lieu d'etre tous disponibles."),
  ("EnableCopyPaste", "Copier-coller", "bool", "Mode creatif. A laisser desactive en survie."),
  ("BlueprintShare", "Partage de plans", "bool", ""),
  ("BlueprintShareTimeout", "Delai de partage de plans (s)", "num", ""),
  ("FamilySharing", "Partage familial Steam", "bool", ""),
  ("CanJoinRunning", "Rejoindre en cours de partie", "bool", "Ne concerne que les scenarios."),
  ("WorldSizeKm", "Taille du monde (km)", "num", "0 = illimite. Au-dela, une barriere invisible."),
  ("MinimumWorldSize", "Taille minimale du monde (km)", "num", ""),
  ("Scenario", "Monde de scenario", "bool", "Change les regles de session. Ne pas activer sur un monde de survie."),
  ("ScenarioEditMode", "Mode edition de scenario", "bool", "Ne pas activer sur un monde de survie."),
 ]),
 ("Mecanique", [
  ("EnableShareInertiaTensor", "Partage du tenseur d'inertie", "bool",
   "Quand c'est desactive, la case du meme nom N'APPARAIT PAS sur les rotors, pistons et "
   "charnieres : le reglage du monde commande l'affichage de l'option sur le bloc. Active, "
   "il stabilise les bras et les grues, au prix de calculs de physique en plus."),
  ("EnableSubgridDamage", "Degats entre sous-grilles", "bool",
   "Une sous-grille qui heurte sa grille mere lui fait des degats. Desactive, les bras "
   "articules cessent de detruire ce qu'ils touchent."),
  ("EnableUnsafeRotorTorques", "Couples de rotor sans limite", "bool",
   "Leve le plafond de couple. Sert aux grosses machines, et c'est aussi ce qui envoie une "
   "construction dans l'espace quand la physique decroche."),
  ("EnableUnsafePistonImpulses", "Impulsions de piston sans limite", "bool", "Meme chose pour les pistons."),
  ("AdjustableMaxVehicleSpeed", "Vitesse max des vehicules ajustable", "bool", ""),
 ]),
 ("Blocs et limites", [
  ("TotalPCU", "PCU total", "num", "Budget de blocs du monde. A monter pour les tres grosses constructions."),
  ("UseConsolePCU", "PCU version console", "bool", "Impose le bareme PCU des consoles. Coherent avec un monde crossplay."),
  ("MaxGridSize", "Taille max d'une grille", "num", "0 = illimite."),
  ("BlockLimitsEnabled", "Limites de blocs", "choix", "", ["NONE", "GLOBALLY", "PER_PLAYER", "PER_FACTION"]),
  ("MaxBlocksPerPlayer", "Blocs max par joueur", "num", "0 = illimite."),
  ("MaxFloatingObjects", "Objets flottants max", "num", "Debris libres toleres avant nettoyage."),
  ("MaxBackupSaves", "Sauvegardes de secours", "num", ""),
  ("MaxProductionQueueLength", "File de production max", "num", ""),
  ("BlockCountThreshold", "Seuil de comptage de blocs", "num", "Sous ce nombre, une grille compte comme un debris."),
  ("OptimalGridCount", "Nombre de grilles optimal", "num", "0 = pas de cible."),
  ("StationVoxelSupport", "Stations ancrees au voxel", "bool", "Une station posee sur du terrain reste immobile sans support."),
  ("EnablePcuTrading", "Echange de PCU entre joueurs", "bool", ""),
 ]),
 ("Armes et degats", [
  ("WeaponsEnabled", "Armes", "bool", ""),
  ("EnableFriendlyFire", "Tir allie", "bool", ""),
  ("EnableTurretsFriendlyFire", "Tourelles tirent sur les allies", "bool", ""),
  ("InfiniteAmmo", "Munitions infinies", "bool", ""),
  ("EnableRecoil", "Recul des armes", "bool", ""),
  ("ThrusterDamage", "Degats des propulseurs", "bool", "Le souffle abime ce qui est derriere."),
  ("EnableVoxelDestruction", "Destruction du voxel", "bool", "Les explosions et les forages creusent le terrain."),
 ]),
 ("PNJ et rencontres", [
  ("EnableEncounters", "Rencontres spatiales", "bool", ""),
  ("EnablePlanetaryEncounters", "Rencontres planetaires", "bool", ""),
  ("CargoShipsEnabled", "Cargos", "bool", "Vaisseaux de transport qui traversent la carte."),
  ("EnableOrca", "Orca", "bool", ""),
  ("EnableDrones", "Drones", "bool", ""),
  ("MaxDrones", "Drones max", "num", ""),
  ("TotalBotLimit", "Bots max", "num", ""),
  ("EnableSpiders", "Araignees", "bool", "Un mod de rencontres peut reprendre ce reglage a son compte."),
  ("EnableWolfs", "Loups", "bool", "Un mod de rencontres peut reprendre ce reglage a son compte."),
  ("EnableContainerDrops", "Largages de conteneurs", "bool", ""),
  ("MinDropContainerRespawnTime", "Delai min entre largages (min)", "num", ""),
  ("MaxDropContainerRespawnTime", "Delai max entre largages (min)", "num", ""),
  ("ScrapEnabled", "Epaves et ferraille", "bool", ""),
  ("NPCGridClaimTimeLimit", "Appropriation d'une grille PNJ (min)", "num", ""),
  ("PiratePCU", "PCU des pirates", "num", "Budget de blocs alloue aux constructions pirates."),
  ("EncounterDensity", "Densite des rencontres", "num", ""),
  ("EncounterGeneratorVersion", "Version du generateur de rencontres", "num", "Technique. Ne pas changer sur un monde en cours."),
  ("GlobalEncounterCap", "Rencontres globales max", "num", "0 = illimite."),
  ("GlobalEncounterPCU", "PCU des rencontres globales", "num", ""),
  ("GlobalEncounterTimer", "Periode des rencontres globales (min)", "num", ""),
  ("GlobalEncounterEnableRemovalTimer", "Retrait auto des rencontres globales", "bool", ""),
  ("GlobalEncounterMinRemovalTimer", "Retrait global, minimum (min)", "num", ""),
  ("GlobalEncounterMaxRemovalTimer", "Retrait global, maximum (min)", "num", ""),
  ("GlobalEncounterRemovalTimeClock", "Horloge de retrait global (min)", "num", ""),
  ("PlanetaryEncounterTimerFirst", "Premiere rencontre planetaire (min)", "num", ""),
  ("PlanetaryEncounterTimerMin", "Rencontre planetaire, minimum (min)", "num", ""),
  ("PlanetaryEncounterTimerMax", "Rencontre planetaire, maximum (min)", "num", ""),
  ("PlanetaryEncounterDespawnTimeout", "Disparition d'une rencontre planetaire (min)", "num", ""),
  ("PlanetaryEncounterDesiredSpawnRange", "Distance d'apparition visee (m)", "num", ""),
  ("PlanetaryEncounterPresenceRange", "Rayon de presence (m)", "num", ""),
  ("PlanetaryEncounterAreaLockdownRange", "Rayon de verrouillage de zone (m)", "num", ""),
  ("PlanetaryEncounterExistingStructuresRange", "Distance aux constructions existantes (m)", "num", ""),
 ]),
 ("Economie et factions", [
  ("EnableEconomy", "Economie", "bool", "Stations commerciales et contrats."),
  ("EconomyTickInSeconds", "Periode economique (s)", "num", "Intervalle entre deux mises a jour des prix et des stocks."),
  ("EnableBountyContracts", "Contrats de prime", "bool", ""),
  ("TradeFactionsCount", "Factions marchandes", "num", ""),
  ("MaxFactionsCount", "Factions max", "num", "0 = illimite."),
  ("ReputationDecayRate", "Decroissance de reputation", "num", ""),
  ("EnableFactionVoiceChat", "Chat vocal de faction", "bool", ""),
  ("EnableTeamBalancing", "Equilibrage des equipes", "bool", ""),
  ("EnableTeamScoreCounters", "Compteurs de score d'equipe", "bool", ""),
 ]),
 ("Environnement", [
  ("WeatherSystem", "Meteo", "bool", ""),
  ("WeatherLightingDamage", "Degats de la foudre", "bool", ""),
  ("EnableSunRotation", "Rotation du soleil", "bool", "Cycle jour/nuit. Sans elle, l'eclairage et les panneaux solaires sont figes."),
  ("SunRotationIntervalMinutes", "Duree d'un cycle solaire (min)", "num", ""),
  ("SolarRadiationIntensity", "Intensite du rayonnement solaire", "num", ""),
  ("FloraDensityMultiplier", "Densite de vegetation", "num", ""),
  ("MaxPlanets", "Planetes max", "num", ""),
  ("ResetForageableItems", "Reapparition des ressources ramassables", "bool", "Baies, branches et pierres au sol."),
  ("ResetForageableItemsTimeM", "Delai de reapparition (min)", "num", ""),
  ("ResetForageableItemsDistance", "Distance de reapparition (m)", "num", "Il faut s'eloigner d'autant pour qu'elles reviennent."),
 ]),
 ("Voxels et terrain", [
  ("EnableVoxelHand", "Main a voxel", "bool", "Outil de terraformage. Creatif."),
  ("PredefinedAsteroids", "Asteroides predefinis", "bool", "Champ d'asteroides pose a la creation, au lieu du procedural."),
  ("ProceduralDensity", "Densite procedurale", "num", "Quantite d'asteroides generes autour des joueurs."),
  ("ProceduralSeed", "Graine procedurale", "num",
   "NE PAS CHANGER sur un monde en cours : tout le terrain jamais visite serait regenere "
   "autrement."),
  ("RandomizeSeed", "Regenerer la graine", "bool", "Tire une nouvelle graine au prochain chargement. Meme consequence."),
  ("DepositSizeDenominator", "Diviseur de taille des gisements", "num", "Plus grand = gisements plus petits."),
  ("DepositsCountCoefficient", "Coefficient du nombre de gisements", "num", ""),
  ("VoxelGeneratorVersion", "Version du generateur de voxel", "num", "Technique. Changer regenere le terrain non visite."),
  ("StationsDistanceInnerRadius", "Rayon interne des stations (m)", "num", ""),
  ("StationsDistanceOuterRadiusStart", "Debut du rayon externe (m)", "num", ""),
  ("StationsDistanceOuterRadiusEnd", "Fin du rayon externe (m)", "num", ""),
  ("OptimalSpawnDistance", "Distance d'apparition optimale (m)", "num", ""),
 ]),
 ("Nettoyage", [
  ("TrashRemovalEnabled", "Nettoyage des debris", "bool", "A LAISSER ACTIF. Sinon les epaves s'accumulent sans fin."),
  ("TrashFlagsValue", "Drapeaux de nettoyage", "num", "Champ de bits, chaque bit une categorie a nettoyer. Le jeu n'en expose pas le detail."),
  ("VoxelTrashRemovalEnabled", "Nettoyage des voxels", "bool", "Efface les trous de forage abandonnes."),
  ("VoxelAgeThreshold", "Age du voxel avant nettoyage (h)", "num", ""),
  ("VoxelGridDistanceThreshold", "Distance voxel/grille (m)", "num", ""),
  ("VoxelPlayerDistanceThreshold", "Distance voxel/joueur (m)", "num", ""),
  ("MaxCargoBags", "Sacs de cargaison max", "num", ""),
  ("TrashCleanerCargoBagsMaxLiveTime", "Duree de vie des sacs (min)", "num", ""),
  ("TemporaryContainers", "Conteneurs temporaires", "bool", ""),
  ("PlayerDistanceThreshold", "Distance au joueur (m)", "num", "En dessous, une grille n'est jamais nettoyee."),
  ("PlayerCharacterRemovalThreshold", "Retrait d'un personnage deconnecte (min)", "num", ""),
  ("PlayerInactivityThreshold", "Inactivite avant nettoyage (h)", "num", "0 = jamais."),
  ("RemoveOldIdentitiesH", "Suppression des vieilles identites (h)", "num", "0 = jamais. Efface les joueurs qui ne reviennent plus."),
  ("EnableTrashSettingsPlatformOverride", "Reglages de nettoyage imposes par la plateforme", "bool", "Les consoles imposent leurs propres seuils."),
  ("ResetOwnership", "Reinitialiser les proprietaires", "bool", "ACTION UNIQUE : au prochain chargement, tous les blocs deviennent sans proprietaire."),
  ("UpdateRespawnDictionary", "Mettre a jour le dictionnaire de reapparition", "bool", "Action technique unique."),
 ]),
 ("Stockage de grilles", [
  ("GridStorageAllowsInventory", "Stockage avec inventaire", "bool", "Ranger une grille sans la vider."),
  ("GridStorageMaxPerPlayer", "Grilles rangees max par joueur", "num", ""),
  ("GridStorageMaxDistance", "Distance max (m)", "num", "0 = illimite."),
  ("GridStorageQueueLimit", "File d'attente max", "num", ""),
  ("GridStorageCombatCooldown", "Delai apres combat (s)", "num", "Empeche de ranger une grille pour fuir un combat."),
  ("GridStorageRetrievalTimeMinMinutes", "Recuperation, minimum (min)", "num", ""),
  ("GridStorageRetrievalTimeMaxMinutes", "Recuperation, maximum (min)", "num", ""),
  ("GridStorageRetrievalTimeMultiplier", "Multiplicateur de recuperation", "num", ""),
  ("GridStorageMinutesPerKm", "Minutes par km", "num", ""),
  ("GridStorageMinutesPerPCU", "Minutes par PCU", "num", ""),
  ("GridStorageExpediteFactor", "Facteur d'acceleration", "num", "Part du temps economisee en payant."),
  ("GridStorageExpediteCostPerSecond", "Cout d'acceleration par seconde", "num", ""),
 ]),
 ("Multijoueur", [
  ("SyncDistance", "Distance de synchronisation (m)", "num",
   "PLAFONNE A 2000 par la compatibilite console. C'est pour ca que la physique selective "
   "doit rester desactivee : Modular Encounters casse en dessous de 10000."),
  ("BroadcastControllerMaxOfflineTransmitDistance", "Portee de l'antenne hors ligne (m)", "num", ""),
  ("AFKTimeountMin", "Expulsion pour inactivite (min)", "num", "0 = jamais. Le nom du reglage porte une faute cote jeu, c'est bien AFK Timeout."),
 ]),
 ("Performance", [
  ("StopGridsPeriodMin", "Gel des grilles (min)", "num", "Fige les grilles inactives depuis N minutes."),
  ("EnableSelectivePhysicsUpdates", "Physique selective", "bool",
   "INCOMPATIBLE avec les mods de rencontres tant que SyncDistance est plafonne a 2000. "
   "Laisser desactive."),
  ("ViewDistance", "Distance de vue", "num", ""),
  ("PhysicsIterations", "Iterations de physique", "num", "Plus haut = plus stable et plus lourd. La simulation reste mono-thread."),
  ("SimplifiedSimulation", "Simulation simplifiee", "bool", ""),
  ("AdaptiveSimulationQuality", "Qualite de simulation adaptative", "bool", "Le serveur baisse la qualite quand il peine."),
  ("PrefetchShapeRayLengthLimit", "Limite de prechargement (m)", "num", ""),
 ]),
 ("Match", [
  ("EnableMatchComponent", "Composant de match", "bool", "Regles de partie a manches. Sans objet sur un monde de survie."),
  ("MatchDuration", "Duree du match (s)", "num", "0 = illimite."),
  ("PreMatchDuration", "Duree d'avant-match (s)", "num", ""),
  ("PostMatchDuration", "Duree d'apres-match (s)", "num", ""),
  ("MatchRestartWhenEmptyTime", "Redemarrage quand vide (s)", "num", "0 = jamais."),
 ]),
]

PLATS = [(k, l, t, a, (c[0] if c else None)) for _, g in REGLAGES for (k, l, t, a, *c) in g]
TYPES = {k: (t, c) for (k, l, t, a, c) in PLATS}

# Reglages qui cassent une partie ou un serveur si on les touche a l'aveugle.
RISQUES = {
    "PermanentDeath": "critique",
    "EnableSaving": "critique",
    "ResetOwnership": "critique",
    "ProceduralSeed": "critique",
    "RandomizeSeed": "critique",
    "VoxelGeneratorVersion": "critique",
    "TrashRemovalEnabled": "critique",
    "EnableSelectivePhysicsUpdates": "critique",
    "GameMode": "critique",
    "Scenario": "critique",
    "ScenarioEditMode": "critique",
    "EnableRespawnShips": "attention",
    "EnvironmentHostility": "attention",
    "EnableCopyPaste": "attention",
    "EnableIngameScripts": "attention",
    "AutoSaveInMinutes": "attention",
    "EnableUnsafeRotorTorques": "attention",
    "EnableUnsafePistonImpulses": "attention",
    "EnableSupergridding": "attention",
    "SyncDistance": "attention",
    "EncounterGeneratorVersion": "attention",
    "UpdateRespawnDictionary": "attention",
    "RemoveOldIdentitiesH": "attention",
    "WorldSizeKm": "attention",
    "EnableSpectator": "attention",
}

# --------------------------------------------------------------------------
# Lecture / ecriture des fichiers du monde
# --------------------------------------------------------------------------


def probleme():
    """Message clair si l'installation n'est pas la ou on la cherche."""
    if not INST.is_dir():
        return ("Dossier d'instance introuvable : " + str(INST) +
                "  —  verifie SE_PREFIX / SE_WINE_USER, ou lance le serveur une premiere fois.")
    if not MONDE_DIR.is_dir():
        dispo = sorted(p.name for p in (INST / "Saves").glob("*") if p.is_dir()) if (INST / "Saves").is_dir() else []
        suite = ("  —  mondes presents : " + ", ".join(dispo)) if dispo else "  —  aucun monde dans Saves/."
        return ("Monde \"" + MONDE + "\" introuvable dans " + str(INST / "Saves") +
                "  —  corrige SE_WORLD." + suite)
    if not LECTURE.is_file():
        return "Fichier de reglages introuvable : " + str(LECTURE)
    return None


def lire():
    if probleme():
        return {}
    s = LECTURE.read_text(encoding="utf-8", errors="replace")
    out = {}
    for k, _, _, _, _ in PLATS:
        m = re.search(r"<" + k + r">([^<]*)</" + k + r">", s)
        if m:
            out[k] = m.group(1)
    return out


def valider(vals):
    """N'ecrit que des valeurs conformes au schema (les fichiers sont du XML)."""
    propres, refuses = {}, []
    for k, v in vals.items():
        if k not in TYPES:
            continue
        t, choix = TYPES[k]
        v = str(v).strip()
        if t == "bool":
            b = v.lower()
            if b in ("true", "1", "on"):
                propres[k] = "true"
            elif b in ("false", "0", "off"):
                propres[k] = "false"
            else:
                refuses.append(k)
        elif t == "num":
            if re.fullmatch(r"-?\d+(?:\.\d+)?", v):
                propres[k] = v
            else:
                refuses.append(k)
        elif t == "choix":
            if choix and v in choix:
                propres[k] = v
            else:
                refuses.append(k)
    return propres, refuses


def ecrire(vals):
    modifies = []
    for f in FICHIERS:
        if not f.exists():
            continue
        t = f.read_text(encoding="utf-8", errors="replace")
        touche = False
        for k, v in vals.items():
            neuf, n = re.subn(r"<" + k + r">[^<]*</" + k + r">", "<" + k + ">" + v + "</" + k + ">", t)
            if n:
                t, touche = neuf, True
        if touche:
            f.write_text(t, encoding="utf-8")
            modifies.append(f.name)
    return modifies


def supprimer_sbsb5():
    """Supprime SANDBOX_0_0_0_.sbsB5 apres une edition du monde.

    Space Engineers relit cette copie compressee obsolete de preference et
    ignore alors silencieusement l'edition qu'on vient de faire.
    scripts/enable-scripts.sh la supprime pour la meme raison, apres avoir
    ecrit dans les memes fichiers.

    Renvoie None si rien n'etait a supprimer, sinon un message a afficher.
    """
    cible = MONDE_DIR / "SANDBOX_0_0_0_.sbsB5"
    if not cible.exists():
        return None
    try:
        cible.unlink()
        return "copie compressee SANDBOX_0_0_0_.sbsB5 supprimee"
    except OSError as e:
        return ("ATTENTION : SANDBOX_0_0_0_.sbsB5 n'a pas pu etre supprime (" +
                str(e) + ") — le jeu risque d'ignorer les reglages ecrits")


# --------------------------------------------------------------------------
# Etat du serveur
# --------------------------------------------------------------------------
_ARRIVEE = re.compile(r"(?i)\b(?:player|user|client)\s*[:=]?\s*[\"']?([^\"'\n]{1,48}?)[\"']?\s+(?:has\s+)?(?:joined|connected)\b")
_DEPART = re.compile(r"(?i)\b(?:player|user|client)\s*[:=]?\s*[\"']?([^\"'\n]{1,48}?)[\"']?\s+(?:has\s+)?(?:left|disconnected|logged\s+out)\b")


def joueurs_connectes():
    """Nombre de joueurs presents, d'apres le compteur du serveur lui-meme.

    Le serveur ecrit toutes les 60 s un bloc STATISTICS precede de sa legende,
    dont une colonne est GetOnlinePlayerCount. C'est SA valeur, pas une
    reconstitution : c'est ce que lit stop.sh, et le panneau doit lire la meme
    chose. Rejouer les arrivees et les departs, comme ici avant, donnait None
    sur un journal ou aucune des deux formulations n'apparaissait, et le
    panneau annoncait alors "inconnu" sur un serveur ou quelqu'un jouait.

    Ne sert que de secours desormais. Renvoie None quand rien n'est lisible :
    l'appelant doit traiter None comme "peut-etre des joueurs", jamais comme 0.
    """
    try:
        logs = [p for p in INST.glob("*.log") if p.is_file()]
        if not logs:
            return None
        recent = max(logs, key=lambda p: p.stat().st_mtime)
        with recent.open("rb") as fh:
            taille = recent.stat().st_size
            fh.seek(max(0, taille - 512 * 1024))
            texte = fh.read().decode("utf-8", errors="replace")

        legende, valeurs = None, None
        for ligne in texte.splitlines():
            if "STATISTICS LEGEND," in ligne:
                legende = ligne.split("STATISTICS LEGEND,", 1)[1].split(",")
                valeurs = None
            elif "STATISTICS," in ligne and legende is not None:
                valeurs = ligne.split("STATISTICS,", 1)[1].split(",")
        if legende and valeurs:
            paires = dict(zip([c.strip() for c in legende], valeurs))
            brut = paires.get("GetOnlinePlayerCount")
            if brut is not None:
                try:
                    return int(float(brut))
                except ValueError:
                    pass

        # Secours : rejouer arrivees et departs. Moins fiable, garde pour les
        # journaux ou le bloc de statistiques n'a pas encore ete ecrit.
        vus, trouve = set(), False
        for ligne in texte.splitlines():
            m = _ARRIVEE.search(ligne)
            if m:
                vus.add(m.group(1).strip())
                trouve = True
                continue
            m = _DEPART.search(ligne)
            if m:
                vus.discard(m.group(1).strip())
                trouve = True
        return len(vus) if trouve else None
    except Exception:
        return None


def _pid_du_jeu():
    """PID du jeu, et de lui seul, ou None.

    `pgrep -f` cherche dans la ligne de commande entiere : le processus tmux
    porte la commande wine dans ses arguments et correspond donc lui aussi.
    Se contenter de "pgrep a trouve quelque chose" ferait dire "en ligne" a
    une session tmux survivante dont le jeu est mort depuis longtemps. On
    filtre sur le NOM du processus. Meme raison que se_pid() dans common.sh.
    """
    r = subprocess.run(["pgrep", "-f", r"SpaceEngineersDedicated\.exe"],
                       capture_output=True, text=True)
    for pid in r.stdout.split():
        nom = subprocess.run(["ps", "-o", "comm=", "-p", pid],
                             capture_output=True, text=True).stdout.strip()
        if nom.endswith("SpaceEngineersDedicated.exe"):
            return pid
    return None


def etat(valeurs=None):
    en_ligne = _pid_du_jeu() is not None
    sbs = MONDE_DIR / "SANDBOX_0_0_0_.sbs"
    age = int((time.time() - sbs.stat().st_mtime) // 60) if sbs.exists() else -1
    maxi = None
    if valeurs:
        try:
            maxi = int(float(valeurs.get("MaxPlayers", "")))
        except (TypeError, ValueError):
            maxi = None
    return {
        "enLigne": en_ligne,
        "ageSauvegarde": age,
        "joueurs": (joueurs_connectes() if en_ligne else 0),
        "joueursMax": maxi,
        "monde": MONDE,
        "erreur": probleme(),
    }


def _taille_lisible(n):
    """Taille en octets rendue courte, sans dependance."""
    if n < 1024:
        return str(n) + " o"
    for unite, div in (("Ko", 1024), ("Mo", 1024 ** 2), ("Go", 1024 ** 3)):
        if n < div * 1024 or unite == "Go":
            return "%.1f %s" % (n / div, unite)
    return str(n) + " o"


def _poids(dossier):
    """Somme des fichiers d'un dossier, sans descendre indefiniment."""
    total, nb = 0, 0
    try:
        for f in dossier.rglob("*"):
            if f.is_file():
                try:
                    total += f.stat().st_size
                    nb += 1
                except OSError:
                    pass
    except OSError:
        pass
    return total, nb


def sauvegardes(limite=24):
    """Sauvegardes automatiques du monde, la plus recente en tete.

    Space Engineers ecrit dans Saves/<monde>/Backup/<AAAA-MM-JJ HHMMSS>/.
    C'est le NOM du dossier qui date la sauvegarde, pas son mtime : copier ou
    restaurer une sauvegarde change le mtime et donnerait un ordre faux.
    """
    dossier = MONDE_DIR / "Backup"
    out = []
    if dossier.is_dir():
        for d in sorted(dossier.iterdir(), reverse=True):
            if not d.is_dir():
                continue
            try:
                quand = datetime.datetime.strptime(d.name, "%Y-%m-%d %H%M%S")
                horo = quand.strftime("%d/%m %H:%M:%S")
                cle = quand.timestamp()
            except ValueError:
                horo, cle = d.name, 0.0
            octets, nb = _poids(d)
            out.append({"nom": d.name, "quand": horo, "cle": cle,
                        "taille": _taille_lisible(octets), "octets": octets, "fichiers": nb})
    out.sort(key=lambda x: x["cle"], reverse=True)
    total = sum(x["octets"] for x in out)
    vif = MONDE_DIR / "SANDBOX_0_0_0_.sbs"
    return {
        "liste": out[:limite],
        "nombre": len(out),
        "total": _taille_lisible(total),
        "vif": (_taille_lisible(vif.stat().st_size) if vif.is_file() else None),
        "vifAge": (int((time.time() - vif.stat().st_mtime) // 60) if vif.is_file() else -1),
    }


def _noms_de_mods():
    """Noms des mods releves dans les journaux du serveur.

    Secours, pas source principale : le nom du fichier du monde fait foi (voir
    mods()). Le journal sert pour un mod qui n'est PAS dans le monde, donc
    qu'aucun fichier ne nomme : une archive laissee dans cache/ apres un
    retrait, ou une entree ajoutee a la main que le serveur n'a pas encore
    reecrite. Les journaux sont lus du plus ancien au plus recent pour qu'un
    renommage cote mod.io finisse par gagner.
    """
    noms = {}
    try:
        journaux = sorted(INST.glob("SpaceEngineersDedicated_*.log"),
                          key=lambda f: f.stat().st_mtime)
    except OSError:
        return noms
    for log in journaux:
        try:
            texte = log.read_text(errors="replace")
        except OSError:
            continue
        # La forme "Up to date mod" d'abord, la forme detaillee ensuite : quand
        # les deux sont presentes dans un meme journal, la seconde fait foi.
        for motif in (r"mod: Id = (\d+), title = '([^']*)'",
                      r"Id = mod\.io:(\d+), Filename = '[^']*', Name = '([^']*)'"):
            for m in re.finditer(motif, texte):
                noms[m.group(1)] = m.group(2)
    return noms


_MOD_ITEM = re.compile(r"<ModItem([^>]*)>(.*?)</ModItem>", re.S)


def _bloc_mods(texte):
    """Contenu du bloc <Mods>, ou chaine vide."""
    m = re.search(r"<Mods>(.*?)</Mods>", texte, re.S)
    return m.group(1) if m else ""


def _lien_mod(ident, service):
    return ("https://mod.io/g/spaceengineers?_q=" + ident) if service != "Steam" else \
           ("https://steamcommunity.com/sharedfiles/filedetails/?id=" + ident)


def _cache_mods():
    """Archives deja telechargees, taille par identifiant.

    Le serveur range chaque mod dans cache/<id>.zip et n'y touche plus : un
    mod retire du monde y reste. C'est ce qui permet de le remettre sans rien
    retelecharger, et c'est aussi ce qui fait grossir le dossier sans qu'aucun
    ecran ne le signale.
    """
    out = {}
    dossier = INST / "cache"
    if not dossier.is_dir():
        return out
    for z in dossier.glob("*.zip"):
        if z.stem.isdigit():
            try:
                out[z.stem] = z.stat().st_size
            except OSError:
                pass
    return out


def mods():
    """Mods actifs du monde dans l'ordre de chargement, et cache des archives.

    L'ordre compte : Space Engineers applique les mods de haut en bas et le
    dernier gagne en cas de conflit. On le preserve tel qu'il est dans le
    fichier plutot que de trier par nom.

    Le nom lisible est l'attribut FriendlyName du fichier du monde. Celui du
    .cfg ne vaut rien : il est fige a l'ajout et vieillit (il dit encore
    "Reddit Custom Encounters" la ou le monde dit "... - Legacy Version"). Le
    journal ne sert que de secours, pour une entree que le serveur n'a pas
    encore reecrite.

    "dormants" = ce que le cache contient et que le monde ne charge pas. C'est
    la liste des mods remettables sans telechargement, et la mesure de la
    place occupee pour rien.
    """
    try:
        texte = (MONDE_DIR / "Sandbox_config.sbc").read_text(errors="replace")
    except OSError:
        texte = ""
    noms = _noms_de_mods()
    zips = _cache_mods()
    out = []
    for attrs, corps in _MOD_ITEM.findall(_bloc_mods(texte)):
        ident = re.search(r"<PublishedFileId>(\d+)</PublishedFileId>", corps)
        if not ident:
            continue
        i = ident.group(1)
        amical = re.search(r'FriendlyName="([^"]*)"', attrs)
        service = re.search(r"<PublishedServiceName>([^<]*)</PublishedServiceName>", corps)
        s = service.group(1) if service else ""
        out.append({
            "id": i,
            "nom": (html.unescape(amical.group(1)) if amical and amical.group(1) else noms.get(i)),
            "service": s,
            "dependance": bool(re.search(r"<IsDependency>\s*true\s*</IsDependency>", corps)),
            "cache": (_taille_lisible(zips[i]) if i in zips else None),
            "lien": _lien_mod(i, s),
        })
    actifs = {m["id"] for m in out}
    dormants = [{"id": i, "nom": noms.get(i), "taille": _taille_lisible(o), "octets": o,
                 "lien": _lien_mod(i, "mod.io")}
                for i, o in sorted(zips.items(), key=lambda kv: -kv[1]) if i not in actifs]
    return {
        "liste": out,
        "nombre": len(out),
        "sansNom": sum(1 for m in out if not m["nom"]),
        "dormants": dormants,
        "dormantsPoids": _taille_lisible(sum(d["octets"] for d in dormants)),
        "cacheTotal": _taille_lisible(sum(zips.values())),
        "cacheChemin": str(INST / "cache"),
    }


def _mods_sans(texte, ident):
    """Retire du bloc <Mods> toute entree portant cet identifiant.

    La ligne d'indentation part avec l'entree : la laisser ne casserait rien,
    mais chaque passage en ajouterait une de plus.
    """
    m = re.search(r"<Mods>(.*?)</Mods>", texte, re.S)
    if not m:
        return texte, 0
    motif = re.compile(r"[ \t]*<ModItem[^>]*>(?:(?!</ModItem>).)*?"
                       r"<PublishedFileId>" + re.escape(ident) + r"</PublishedFileId>"
                       r".*?</ModItem>[ \t]*\r?\n?", re.S)
    interieur, n = motif.subn("", m.group(1))
    if not n:
        return texte, 0
    return texte[:m.start(1)] + interieur + texte[m.end(1):], n


def _mods_avec(texte, ident, nom, service="mod.io"):
    """Ajoute une entree a la FIN du bloc <Mods>, ou cree le bloc s'il manque.

    A la fin, jamais en tete : le dernier mod charge gagne en cas de conflit,
    donc un ajout en tete changerait le comportement de ceux deja en place.
    """
    entree = ('    <ModItem FriendlyName="' + html.escape(nom, quote=True) + '">\n'
              '      <Name>' + ident + '.sbm</Name>\n'
              '      <PublishedFileId>' + ident + '</PublishedFileId>\n'
              '      <PublishedServiceName>' + service + '</PublishedServiceName>\n'
              '    </ModItem>\n')
    m = re.search(r"<Mods>(.*?)</Mods>", texte, re.S)
    if m:
        interieur = m.group(1)
        # l'indentation qui precede </Mods> doit rester derriere l'ajout
        queue = re.search(r"[ \t]*$", interieur).group(0)
        corps = interieur[:len(interieur) - len(queue)]
        if not corps.endswith("\n"):
            corps += "\n"
        return texte[:m.start(1)] + corps + entree + queue + texte[m.end(1):], True
    vide = re.search(r"<Mods\s*/>", texte)
    if vide:
        return texte[:vide.start()] + "<Mods>\n" + entree + "  </Mods>" + texte[vide.end():], True
    return texte, False


def _sauvegarde_valide(nom):
    """Resout un nom de sauvegarde en chemin, ou None.

    Le nom vient du navigateur : il est resolu puis compare au dossier Backup
    reel. Un nom contenant ../ ou un lien symbolique sortant est donc rejete,
    pas simplement filtre.
    """
    if not nom or "/" in nom or "\\" in nom:
        return None
    base = (MONDE_DIR / "Backup").resolve()
    try:
        cible = (base / nom).resolve()
        cible.relative_to(base)
    except (OSError, ValueError):
        return None
    return cible if cible.is_dir() else None


def reveler(nom, quoi=None):
    """Ouvre le Finder sur une sauvegarde, sur le cache des mods, ou sur le monde.

    Le cache est le seul dossier ou les mods existent vraiment : Mods/ reste
    vide sur une installation dediee, le serveur telecharge dans cache/ sous
    le seul numero du mod.
    """
    if quoi == "cache":
        cible = INST / "cache"
        if not cible.is_dir():
            return {"ok": False, "erreur": "aucun cache de mods : " + str(cible)}
    else:
        cible = _sauvegarde_valide(nom) if nom else MONDE_DIR
    if cible is None:
        return {"ok": False, "erreur": "sauvegarde inconnue"}
    try:
        subprocess.run(["open", "-R", str(cible)], capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        return {"ok": False, "erreur": str(e)[:200]}
    return {"ok": True, "chemin": str(cible)}


def restaurer(nom, forcer=False):
    """Remet le monde dans l'etat d'une sauvegarde.

    Sequence : arreter, mettre l'etat courant de cote, remplacer, relancer.
    L'arret vient en premier parce qu'un monde ecrit pendant que le serveur
    tourne est reecrit par la sauvegarde automatique suivante. Et si l'arret
    est refuse, rien n'a bouge.

    L'etat courant n'est pas efface mais deplace dans Backup/ sous un nom
    horodate : une restauration se defait donc par une autre restauration.
    """
    cible = _sauvegarde_valide(nom)
    if cible is None:
        return {"ok": False, "erreur": "sauvegarde inconnue : " + str(nom)}
    if not (cible / "Sandbox.sbc").is_file():
        return {"ok": False, "erreur": "sauvegarde incomplete, Sandbox.sbc absent"}

    arret, demarrage = ROOT / "scripts/stop.sh", ROOT / "scripts/start.sh"
    for s in (arret, demarrage):
        if not s.is_file():
            return {"ok": False, "erreur": "script introuvable : " + str(s)}

    cmd = ["bash", str(arret)] + (["--force"] if forcer else [])
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=DELAI_ARRET)
    if r.returncode != 0:
        detail = " ".join(((r.stdout or "") + " " + (r.stderr or "")).split())
        return {"ok": False, "arretRefuse": True,
                "erreur": "arret refuse, le monde n'a pas ete touche : " + detail[:500]}

    horo = datetime.datetime.now().strftime("%Y-%m-%d %H%M%S")
    filet = MONDE_DIR / "Backup" / (horo + " avant-restauration")
    try:
        filet.mkdir(parents=True, exist_ok=False)
        for f in MONDE_DIR.iterdir():
            if f.name == "Backup":
                continue
            shutil.move(str(f), str(filet / f.name))
        for f in cible.iterdir():
            (shutil.copytree if f.is_dir() else shutil.copy2)(str(f), str(MONDE_DIR / f.name))
        # Le jeu relit de preference la copie compressee : la laisser annulerait
        # silencieusement la restauration.
        sbsb5 = MONDE_DIR / "SANDBOX_0_0_0_.sbsB5"
        if sbsb5.exists():
            sbsb5.unlink()
    except (OSError, shutil.Error) as e:
        subprocess.run(["bash", str(demarrage)], capture_output=True, timeout=DELAI_DEMARRAGE)
        return {"ok": False, "erreur": "restauration interrompue : " + str(e)[:300] +
                "  —  l'etat precedent est dans Backup/" + filet.name}

    subprocess.run(["bash", str(demarrage)], capture_output=True, timeout=DELAI_DEMARRAGE)
    return {"ok": True, "restauree": nom, "filet": filet.name}


# --------------------------------------------------------------------------
# Identite du serveur et joueurs
# --------------------------------------------------------------------------
# Trois fichiers, trois roles distincts :
#   SpaceEngineers-Dedicated.cfg  nom du serveur, nom affiche du monde,
#                                 empreinte du mot de passe, liste des bannis
#   Saves/<monde>/Sandbox.sbc     joueurs connus et leur niveau de promotion
# Le .cfg n'est relu qu'au demarrage, et Sandbox.sbc est reecrit depuis la
# memoire a chaque sauvegarde automatique : toute ecriture passe donc par un
# arret, comme pour les reglages.

CFG_FICHIER = "SpaceEngineers-Dedicated.cfg"


def _cfg_texte():
    return (INST / CFG_FICHIER).read_text(encoding="utf-8", errors="replace")


def _champ(texte, nom):
    m = re.search(r"<" + nom + r">([^<]*)</" + nom + r">", texte)
    return m.group(1) if m else None


def _pose_champ(texte, nom, valeur):
    """Remplace un champ simple, ou l'ajoute s'il est absent ou auto-fermant."""
    v = html.escape(valeur, quote=False)
    if re.search(r"<" + nom + r">[^<]*</" + nom + r">", texte):
        return re.sub(r"<" + nom + r">[^<]*</" + nom + r">", "<" + nom + ">" + v + "</" + nom + ">", texte, count=1)
    if re.search(r"<" + nom + r"\s*/>", texte):
        return re.sub(r"<" + nom + r"\s*/>", "<" + nom + ">" + v + "</" + nom + ">", texte, count=1)
    return texte


def empreinte_mot_de_passe(mdp):
    """Sel et cle PBKDF2, en base64, tels que le serveur les attend.

    Deux champs DISTINCTS : <ServerPasswordSalt> et <ServerPasswordHash>.
    Ce ne sont pas une seule valeur concatenee, et <ServerPassword> en clair
    n'existe pas. Le mot de passe ne quitte jamais la machine.
    """
    sel = os.urandom(16)
    cle = hashlib.pbkdf2_hmac("sha1", mdp.encode("utf-8"), sel, 10000, 20)
    return base64.b64encode(sel).decode(), base64.b64encode(cle).decode()


def _bannis(texte):
    m = re.search(r"<Banned\s*/>|<Banned>(.*?)</Banned>", texte, re.S)
    if not m or not m.group(1):
        return []
    return re.findall(r"<unsignedLong>(\d+)</unsignedLong>", m.group(1))


def _pose_bannis(texte, ids):
    if not ids:
        bloc = "<Banned />"
    else:
        bloc = "<Banned>\n" + "".join(
            "    <unsignedLong>" + i + "</unsignedLong>\n" for i in ids) + "  </Banned>"
    if re.search(r"<Banned\s*/>", texte):
        return re.sub(r"<Banned\s*/>", bloc, texte, count=1)
    return re.sub(r"<Banned>.*?</Banned>", bloc, texte, flags=re.S, count=1)


def administration():
    """Identite du serveur et joueurs connus du monde."""
    try:
        cfg = _cfg_texte()
    except OSError as e:
        return {"erreur": str(e)[:200], "joueurs": [], "identite": {}}
    bannis = set(_bannis(cfg))
    joueurs = []
    try:
        sbc = (MONDE_DIR / "Sandbox.sbc").read_text(encoding="utf-8", errors="replace")
    except OSError:
        sbc = ""
    motif = (r"<item>\s*<Key>\s*<ClientId>(\d+)</ClientId>\s*<SerialId>(\d+)</SerialId>\s*"
             r"<HashedId>(\d+)</HashedId>\s*</Key>\s*<Value>\s*<DisplayName>([^<]*)</DisplayName>\s*"
             r"<IdentityId>(\d+)</IdentityId>(.*?)</Value>")
    for m in re.finditer(motif, sbc, re.S):
        niveau = re.search(r"<PromoteLevel>([^<]*)</PromoteLevel>", m.group(6))
        # Space Engineers prefixe le pseudo d'un glyphe de zone privee (le badge
        # de plateforme). Il n'existe dans aucune police d'interface et s'affiche
        # en carre vide : on le retire de l'affichage, pas du fichier.
        nom = re.sub(r"[\ue000-\uf8ff]", "", m.group(4)).strip()
        joueurs.append({
            "nom": nom or m.group(4),
            "hashedId": m.group(3),
            "identityId": m.group(5),
            "niveau": (niveau.group(1) if niveau else "None"),
            "banni": m.group(3) in bannis,
        })
    # un banni qui n'est plus dans la sauvegarde reste listable, sinon il
    # devient impossible de le debannir depuis le panneau
    connus = {j["hashedId"] for j in joueurs}
    for b in bannis - connus if isinstance(bannis, set) else []:
        joueurs.append({"nom": "(inconnu du monde)", "hashedId": b,
                        "identityId": "", "niveau": "None", "banni": True})
    return {
        "identite": {
            "ServerName": _champ(cfg, "ServerName") or "",
            "WorldName": _champ(cfg, "WorldName") or "",
            "motDePasse": bool(_champ(cfg, "ServerPasswordHash")),
        },
        "joueurs": joueurs,
    }


def appliquer_administration(action, forcer=False, **kw):
    """Ecrit une modification d'identite, de promotion ou de bannissement.

    Comme pour les reglages : arreter d'abord, ecrire ensuite, relancer. Le
    .cfg n'est relu qu'au demarrage et Sandbox.sbc est reecrit depuis la
    memoire a chaque sauvegarde : ecrire pendant que le serveur tourne serait
    annule sans le moindre message.
    """
    arret, demarrage = ROOT / "scripts/stop.sh", ROOT / "scripts/start.sh"
    for s in (arret, demarrage):
        if not s.is_file():
            return {"ok": False, "erreur": "script introuvable : " + str(s)}

    r = subprocess.run(["bash", str(arret)] + (["--force"] if forcer else []),
                       capture_output=True, text=True, timeout=DELAI_ARRET)
    if r.returncode != 0:
        detail = " ".join(((r.stdout or "") + " " + (r.stderr or "")).split())
        return {"ok": False, "arretRefuse": True,
                "erreur": "arret refuse, rien n'a ete ecrit : " + detail[:500]}

    resume = []
    try:
        cfg_f = INST / CFG_FICHIER
        cfg = _cfg_texte()

        if action == "identite":
            for champ in ("ServerName", "WorldName"):
                v = kw.get(champ)
                if v is not None and v != "":
                    cfg = _pose_champ(cfg, champ, v)
                    resume.append(champ + " = " + v)
            cfg_f.write_text(cfg, encoding="utf-8")

        elif action == "motdepasse":
            mdp = kw.get("motDePasse") or ""
            if mdp:
                sel, cle = empreinte_mot_de_passe(mdp)
                cfg = _pose_champ(cfg, "ServerPasswordSalt", sel)
                cfg = _pose_champ(cfg, "ServerPasswordHash", cle)
                resume.append("mot de passe change")
            else:
                cfg = _pose_champ(cfg, "ServerPasswordSalt", "")
                cfg = _pose_champ(cfg, "ServerPasswordHash", "")
                resume.append("mot de passe retire")
            cfg_f.write_text(cfg, encoding="utf-8")

        elif action == "bannir":
            cible, etat = str(kw.get("hashedId") or ""), bool(kw.get("banni"))
            if not cible.isdigit():
                raise ValueError("identifiant de joueur invalide")
            ids = _bannis(cfg)
            ids = ([i for i in ids if i != cible] + ([cible] if etat else []))
            cfg_f.write_text(_pose_bannis(cfg, ids), encoding="utf-8")
            resume.append(("banni " if etat else "debanni ") + cible)

        elif action == "promouvoir":
            cible, niveau = str(kw.get("hashedId") or ""), kw.get("niveau")
            if niveau not in ("None", "Admin"):
                raise ValueError("niveau attendu : None ou Admin")
            f = MONDE_DIR / "Sandbox.sbc"
            sbc = f.read_text(encoding="utf-8", errors="replace")
            motif = (r"(<HashedId>" + re.escape(cible) + r"</HashedId>.*?<PromoteLevel>)"
                     r"([^<]*)(</PromoteLevel>)")
            neuf, n = re.subn(motif, lambda m: m.group(1) + niveau + m.group(3), sbc, count=1, flags=re.S)
            if not n:
                raise ValueError("joueur introuvable dans la sauvegarde : " + cible)
            f.write_text(neuf, encoding="utf-8")
            b5 = MONDE_DIR / "SANDBOX_0_0_0_.sbsB5"
            if b5.exists():
                b5.unlink()
            resume.append(cible + " -> " + niveau)
        elif action in ("mod_ajouter", "mod_retirer"):
            # Trois fichiers portent la liste des mods, et ils ne disent pas la
            # meme chose : le .cfg n'a pas les dependances resolues par le jeu.
            # Ecrire un seul des trois laisse une liste qui se contredit.
            ident = str(kw.get("mod") or "").strip()
            if not re.fullmatch(r"\d{1,12}", ident):
                raise ValueError("identifiant de mod attendu : des chiffres uniquement")
            presents = {m["id"] for m in mods()["liste"]}
            touches = []
            if action == "mod_ajouter":
                if ident in presents:
                    raise ValueError("ce mod est deja dans le monde : " + ident)
                nom = ((kw.get("nomMod") or "").strip()
                       or _noms_de_mods().get(ident) or ("mod " + ident))
                for fichier in FICHIERS:
                    if not fichier.exists():
                        continue
                    neuf, pose = _mods_avec(fichier.read_text(encoding="utf-8", errors="replace"),
                                            ident, nom)
                    if pose:
                        fichier.write_text(neuf, encoding="utf-8")
                        touches.append(fichier.name)
                if not touches:
                    raise ValueError("aucun bloc <Mods> trouve, rien n'a ete ecrit")
                resume.append("ajoute " + nom + " (" + ident + ") dans " + ", ".join(touches))
            else:
                if ident not in presents:
                    raise ValueError("ce mod n'est pas dans le monde : " + ident)
                for fichier in FICHIERS:
                    if not fichier.exists():
                        continue
                    neuf, n = _mods_sans(fichier.read_text(encoding="utf-8", errors="replace"),
                                         ident)
                    if n:
                        fichier.write_text(neuf, encoding="utf-8")
                        touches.append(fichier.name)
                resume.append("retire " + ident + " de " + ", ".join(touches) +
                              " (l'archive reste dans cache/)")
            b5 = MONDE_DIR / "SANDBOX_0_0_0_.sbsB5"
            if b5.exists():
                b5.unlink()
        else:
            raise ValueError("action inconnue : " + str(action))

    except (OSError, ValueError, re.error) as e:
        subprocess.run(["bash", str(demarrage)], capture_output=True, timeout=DELAI_DEMARRAGE)
        return {"ok": False, "erreur": str(e)[:300]}

    subprocess.run(["bash", str(demarrage)], capture_output=True, timeout=DELAI_DEMARRAGE)
    return {"ok": True, "resume": resume}


def commander_serveur(action, forcer=False):
    """Demarre, arrete ou redemarre le serveur.

    Le panneau ne lance pas le binaire lui-meme : start.sh pose la session
    tmux, la boucle de reprise et le caffeinate arrime au PID, et stop.sh
    porte le garde-fou d'age de sauvegarde. Doubler l'un des deux ici donnerait
    un serveur sans filet, ou un arret qui perd du travail sans le dire.

    L'etat est relu APRES coup plutot que deduit du code de retour : un script
    qui rend 0 en ayant echoue est plus frequent qu'un serveur qui ment sur sa
    propre presence dans la table des processus.
    """
    if action not in ("demarrer", "arreter", "redemarrer"):
        return {"ok": False, "erreur": "action inconnue : " + str(action)}
    arret, demarrage = ROOT / "scripts/stop.sh", ROOT / "scripts/start.sh"
    for s in (arret, demarrage):
        if not s.is_file():
            return {"ok": False, "erreur": "script introuvable : " + str(s)}

    notes = []
    if action in ("arreter", "redemarrer"):
        if etat()["enLigne"]:
            r = subprocess.run(["bash", str(arret)] + (["--force"] if forcer else []),
                               capture_output=True, text=True, timeout=DELAI_ARRET)
            detail = " ".join(((r.stdout or "") + " " + (r.stderr or "")).split())[:500]
            if r.returncode != 0:
                return {"ok": False, "arretRefuse": True, "enLigne": etat()["enLigne"],
                        "erreur": "arret refuse, le serveur tourne toujours : " + detail}
            notes.append("arrete")
        else:
            notes.append("etait deja arrete")

    if action in ("demarrer", "redemarrer"):
        if etat()["enLigne"]:
            notes.append("tournait deja")
        else:
            # Large : start.sh reessaie SE_START_ATTEMPTS fois, chacune jusqu'a
            # SE_START_TIMEOUT. Couper avant la fin laisserait un serveur en
            # cours de chargement dont le panneau dirait qu'il a echoue.
            subprocess.run(["bash", str(demarrage)], capture_output=True, timeout=DELAI_DEMARRAGE)
            notes.append("demarre")

    en_ligne = etat()["enLigne"]
    attendu = (action != "arreter")
    if en_ligne == attendu:
        return {"ok": True, "enLigne": en_ligne, "note": ", ".join(notes)}
    return {"ok": False, "enLigne": en_ligne, "note": ", ".join(notes),
            "erreur": ("le serveur tourne encore apres l'arret"
                       if en_ligne else
                       "le serveur ne repond pas apres le demarrage — tmux attach -t se, "
                       "ou regarde le dernier journal")}


# --------------------------------------------------------------------------
# Interface
# --------------------------------------------------------------------------
PAGE = r"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Reglages — __MONDE__</title>
<script>try{var m=localStorage.getItem("se-theme");if(m==="dark"||m==="light")document.documentElement.setAttribute("data-theme",m);}catch(e){}</script>
<style>
/* ---- jetons : le theme clair est defini sur :root nu, jamais dans un media ---- */
:root{
  /* Polices : uniquement ce qui est deja installe sur la machine. Aucune
     requete sortante, c'est la garantie affichee par le panneau. */
  --font-sans:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  --font-mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,"DejaVu Sans Mono",monospace;
  --font-cond:"Avenir Next Condensed","Helvetica Neue Condensed","Roboto Condensed",var(--font-sans);
  color-scheme:light;
  --ground:#D9E0E8; --grid:rgba(30,52,74,.055);
  --panel:#F2F5F9; --panel2:#E5EBF2; --inset:#DCE3EB;
  --line:#B4C0CD; --line-soft:#D3DCE5;
  --ink:#101820; --soft:#38434F; --muted:#5C6A79;
  --signal:#B35309; --signal-ink:#FFFFFF; --signal-soft:rgba(179,83,9,.10);
  --hazard:#B8860B; --hazard-soft:rgba(184,134,11,.12);
  --steel:#1D5A6E;
  --ok:#1D6A47; --ok-soft:rgba(29,106,71,.12);
  --stop:#93301C; --stop-soft:rgba(147,48,28,.10);
  --screen:#0E161E; --screen-ink:#D9E7EF; --screen-dim:#7A8FA0; --screen-line:rgba(255,255,255,.07);
  --rivet:rgba(16,24,32,.16);
  --shadow:0 1px 2px rgba(16,28,40,.10), 0 8px 24px -18px rgba(16,28,40,.55);
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  color-scheme:dark;
  --ground:#0B1015; --grid:rgba(140,180,215,.045);
  --panel:#141C25; --panel2:#1B242E; --inset:#0F171F;
  --line:#2B3743; --line-soft:#222D38;
  --ink:#DEE7EF; --soft:#B3C0CD; --muted:#7F8E9E;
  --signal:#E9A055; --signal-ink:#14100A; --signal-soft:rgba(233,160,85,.12);
  --hazard:#D8AE4A; --hazard-soft:rgba(216,174,74,.13);
  --steel:#69AFC6;
  --ok:#72C69A; --ok-soft:rgba(114,198,154,.13);
  --stop:#E38870; --stop-soft:rgba(227,136,112,.13);
  --screen:#080D12; --screen-ink:#CFE0EA; --screen-dim:#6C808F; --screen-line:rgba(255,255,255,.05);
  --rivet:rgba(220,235,250,.13);
  --shadow:0 1px 0 rgba(0,0,0,.4), 0 10px 30px -20px #000;
}}
:root[data-theme="dark"]{
  color-scheme:dark;
  --ground:#0B1015; --grid:rgba(140,180,215,.045);
  --panel:#141C25; --panel2:#1B242E; --inset:#0F171F;
  --line:#2B3743; --line-soft:#222D38;
  --ink:#DEE7EF; --soft:#B3C0CD; --muted:#7F8E9E;
  --signal:#E9A055; --signal-ink:#14100A; --signal-soft:rgba(233,160,85,.12);
  --hazard:#D8AE4A; --hazard-soft:rgba(216,174,74,.13);
  --steel:#69AFC6;
  --ok:#72C69A; --ok-soft:rgba(114,198,154,.13);
  --stop:#E38870; --stop-soft:rgba(227,136,112,.13);
  --screen:#080D12; --screen-ink:#CFE0EA; --screen-dim:#6C808F; --screen-line:rgba(255,255,255,.05);
  --rivet:rgba(220,235,250,.13);
  --shadow:0 1px 0 rgba(0,0,0,.4), 0 10px 30px -20px #000;
}

*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;min-height:100vh;background:var(--ground);color:var(--ink);
  font-family:var(--font-sans);font-size:15px;line-height:1.55;
  background-image:linear-gradient(var(--grid) 1px,transparent 1px),linear-gradient(90deg,var(--grid) 1px,transparent 1px);
  background-size:38px 38px;overflow-x:hidden}
.mono{font-family:var(--font-mono)}
.shell{max-width:74rem;margin:0 auto;padding:0 1.1rem 2rem;display:flex;flex-direction:column;gap:1.6rem}
h1,h2,h3{margin:0}

/* ---- bandeau de coque ---- */
.hull{position:relative;padding:0;background:var(--panel);border-bottom:1px solid var(--line)}
.hazard{height:7px;background:repeating-linear-gradient(115deg,var(--hazard) 0 14px,transparent 14px 28px);
  border-bottom:1px solid var(--line-soft)}
.hull-in{max-width:74rem;margin:0 auto;padding:1.35rem 1.1rem 1.25rem;display:flex;flex-wrap:wrap;
  gap:1rem 1.5rem;align-items:flex-end;justify-content:space-between;position:relative}
.hull-in::before,.hull-in::after{content:"";position:absolute;top:.95rem;width:7px;height:7px;border-radius:50%;
  background:var(--rivet);box-shadow:0 0 0 1px var(--line-soft) inset}
.hull-in::before{left:.55rem}.hull-in::after{right:.55rem}
.eyebrow{font-family:var(--font-mono);font-size:.7rem;font-weight:500;text-transform:uppercase;
  letter-spacing:.2em;color:var(--muted);display:block}
h1{font-family:var(--font-cond);font-size:clamp(1.6rem,4.5vw,2.3rem);
  font-weight:700;letter-spacing:-.005em;line-height:1.1;margin:.15rem 0 .25rem;overflow-wrap:anywhere}
.sub{margin:0;font-size:.83rem;color:var(--muted)}
.sub b{color:var(--soft);font-weight:500}

/* ---- selecteur de theme ---- */
.theme{display:flex;border:1px solid var(--line);border-radius:2px;overflow:hidden;background:var(--panel2)}
.theme button{font-family:var(--font-mono);font-size:.66rem;letter-spacing:.12em;text-transform:uppercase;
  padding:.4rem .6rem;border:0;border-left:1px solid var(--line-soft);background:transparent;color:var(--muted);cursor:pointer}
.theme button:first-child{border-left:0}
.theme button[aria-pressed="true"]{background:var(--signal);color:var(--signal-ink)}

/* ---- ecrans d'etat ---- */
.shell > #inventaire{display:flex;flex-direction:column;gap:1.6rem}
#inventaire section{display:flex;flex-direction:column;gap:.7rem}
.champ-ligne{display:flex;gap:.6rem;align-items:center;flex-wrap:wrap;padding:.5rem .8rem;border-top:1px solid var(--line-soft)}
.champ-ligne:first-child{border-top:0}
.champ-ligne label{font-size:.86rem;min-width:12rem}
.champ-ligne input{flex:1;min-width:12rem}
.champ-ligne .cle{flex-basis:100%;margin:0}
.pastille{font-family:var(--font-mono);font-size:.62rem;letter-spacing:.12em;text-transform:uppercase;
  padding:.14rem .4rem;border-radius:2px;border:1px solid var(--line)}
.pastille.admin{border-color:var(--signal);color:var(--signal)}
.pastille.banni{border-color:var(--stop);color:var(--stop)}
.pastille.dep{border-color:var(--hazard);color:var(--hazard)}
.mini{font-family:var(--font-mono);font-size:.66rem;letter-spacing:.1em;text-transform:uppercase;
  padding:.28rem .55rem;border:1px solid var(--line);border-radius:2px;background:var(--panel2);
  color:var(--soft);cursor:pointer;white-space:nowrap}
.mini:hover{border-color:var(--signal);color:var(--signal)}
.mini.danger{border-color:var(--stop);color:var(--stop)}
.mini.danger:hover{background:var(--stop);color:var(--panel)}
.mini[disabled]{opacity:.45;cursor:default}
.actions{display:flex;gap:.35rem;justify-content:flex-end}
.tbl{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--line);border-radius:3px;overflow:hidden}
.tbl th{font-family:var(--font-mono);font-size:.64rem;letter-spacing:.18em;text-transform:uppercase;
  color:var(--muted);text-align:left;padding:.55rem .8rem;background:var(--panel2);border-bottom:1px solid var(--line)}
.tbl td{padding:.5rem .8rem;border-top:1px solid var(--line-soft);font-size:.86rem;vertical-align:baseline}
.tbl tr:first-child td{border-top:0}
.tbl .num{font-family:var(--font-mono);color:var(--muted);font-size:.76rem;width:2.2rem}
.tbl .droite{text-align:right;font-family:var(--font-mono);font-size:.8rem;color:var(--soft);white-space:nowrap}
.tbl .frais td{background:var(--ok-soft)}
.tbl a{color:var(--steel)}
.sansnom{color:var(--muted);font-style:italic}
.wrap{overflow-x:auto}
.lcds{display:grid;gap:.85rem;grid-template-columns:repeat(auto-fit,minmax(12.5rem,1fr))}
.lcd{position:relative;background:var(--screen);border:1px solid var(--line);border-radius:3px;
  padding:.85rem .95rem .8rem;overflow:hidden;box-shadow:var(--shadow)}
.lcd::after{content:"";position:absolute;inset:0;pointer-events:none;
  background:repeating-linear-gradient(0deg,var(--screen-line) 0 1px,transparent 1px 3px)}
.lcd .k{font-family:var(--font-mono);font-size:.66rem;letter-spacing:.2em;text-transform:uppercase;color:var(--screen-dim)}
.lcd .v{font-family:var(--font-cond);font-size:1.75rem;font-weight:700;
  line-height:1.15;margin-top:.15rem;color:var(--screen-ink);display:flex;align-items:baseline;gap:.45rem;overflow-wrap:anywhere}
.lcd .u{font-family:var(--font-mono);font-size:.8rem;font-weight:400;color:var(--screen-dim)}
.lcd .n{font-family:var(--font-mono);font-size:.7rem;color:var(--screen-dim);margin-top:.2rem;overflow-wrap:anywhere}
.lcd.s-ok{border-left:3px solid var(--ok)} .lcd.s-ok .v{color:var(--ok)}
.lcd.s-warn{border-left:3px solid var(--hazard)} .lcd.s-warn .v{color:var(--hazard)}
.lcd.s-stop{border-left:3px solid var(--stop)} .lcd.s-stop .v{color:var(--stop)}
.dot{width:.55rem;height:.55rem;border-radius:50%;background:currentColor;display:inline-block;flex:0 0 auto;
  align-self:center;box-shadow:0 0 0 3px color-mix(in srgb,currentColor 22%,transparent)}
.pulse{animation:pulse 2.4s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
@media (prefers-reduced-motion:reduce){.pulse{animation:none}}

/* ---- panneaux d'avertissement ---- */
.notice{display:flex;gap:.85rem;align-items:flex-start;background:var(--panel);border:1px solid var(--line);
  border-left:0;border-radius:3px;padding:.85rem 1rem;font-size:.88rem;color:var(--soft);position:relative;overflow:hidden}
.notice::before{content:"";position:absolute;left:0;top:0;bottom:0;width:8px;
  background:repeating-linear-gradient(-45deg,var(--hazard) 0 5px,transparent 5px 10px)}
.notice>div{padding-left:.5rem}
.notice b{color:var(--ink)}
.notice.err::before{background:repeating-linear-gradient(-45deg,var(--stop) 0 5px,transparent 5px 10px)}
.notice.err{color:var(--stop)}
.notice.hide{display:none}
.pilote{display:flex;flex-wrap:wrap;gap:.55rem;align-items:center;background:var(--panel);
  border:1px solid var(--line);border-radius:3px;padding:.6rem .8rem;box-shadow:var(--shadow)}
.pilote .k{font-family:var(--font-mono);font-size:.66rem;letter-spacing:.2em;text-transform:uppercase;
  color:var(--muted);margin-right:.35rem}
.filtre{display:flex;gap:.6rem;align-items:center;background:var(--panel);border:1px solid var(--line);
  border-radius:3px;padding:.6rem .8rem;box-shadow:var(--shadow)}
.filtre input{flex:1;min-width:10rem}
.filtre .n{font-family:var(--font-mono);font-size:.72rem;color:var(--muted);white-space:nowrap}
.row.hide,section.hide{display:none}

/* ---- sections ---- */
form{display:flex;flex-direction:column;gap:1.6rem;margin:0}
section{display:flex;flex-direction:column;gap:.75rem}
.sec-head{display:flex;align-items:center;gap:.7rem}
.sec-num{font-family:var(--font-mono);font-size:.72rem;font-weight:600;letter-spacing:.1em;
  color:var(--signal);border:1px solid var(--line);background:var(--signal-soft);padding:.12rem .4rem;border-radius:2px}
h2{font-family:var(--font-cond);font-size:1.02rem;font-weight:700;
  text-transform:uppercase;letter-spacing:.13em;color:var(--soft);white-space:nowrap}
.sec-rule{flex:1;height:1px;background:var(--line);min-width:1rem}
.sec-count{font-family:var(--font-mono);font-size:.7rem;color:var(--muted)}
.grid{display:grid;gap:.7rem;grid-template-columns:1fr}
@media(min-width:62rem){.grid{grid-template-columns:1fr 1fr}}

/* ---- lignes de reglage ---- */
.row{display:grid;grid-template-columns:minmax(0,1fr);gap:.6rem;background:var(--panel);border:1px solid var(--line);
  border-left:3px solid var(--line);border-radius:3px;padding:.75rem .9rem;min-width:0;box-shadow:var(--shadow)}
@media(min-width:34rem){.row{grid-template-columns:minmax(0,1fr) 8.5rem;gap:1rem;align-items:start}}
.row-main{min-width:0}
.row-top{display:flex;flex-wrap:wrap;align-items:center;gap:.45rem}
.nom{font-weight:600;cursor:pointer}
.cle{display:block;font-family:var(--font-mono);font-size:.72rem;color:var(--muted);
  margin-top:.1rem;overflow-wrap:anywhere}
.aide{font-size:.84rem;color:var(--soft);margin:.4rem 0 0}
.tag{font-family:var(--font-mono);font-size:.62rem;font-weight:600;text-transform:uppercase;letter-spacing:.14em;
  padding:.1rem .38rem;border-radius:2px;border:1px solid currentColor}
.t-crit{color:var(--stop);background:var(--stop-soft)}
.t-warn{color:var(--hazard);background:var(--hazard-soft)}
.row.r-critique{border-left-width:5px;border-left-color:transparent;
  background-image:linear-gradient(var(--panel),var(--panel)),repeating-linear-gradient(-45deg,var(--stop) 0 4px,var(--stop-soft) 4px 8px);
  background-origin:border-box;background-clip:padding-box,border-box}
.row.r-attention{border-left-width:5px;border-left-color:var(--hazard)}
.row.mod{border-color:var(--signal)}
.row.mod .nom::after{content:"";display:inline-block;width:.45rem;height:.45rem;background:var(--signal);
  border-radius:50%;margin-left:.4rem;vertical-align:middle}

/* ---- champs ---- */
.fld{width:100%;padding:.45rem .55rem;border:1px solid var(--line);border-radius:2px;background:var(--inset);
  color:var(--ink);font-family:var(--font-mono);font-size:.9rem;min-width:0}
input.fld{text-align:right}
select.fld{text-align:left;appearance:none;-webkit-appearance:none;padding-right:1.5rem;
  background-image:linear-gradient(45deg,transparent 50%,currentColor 50%),linear-gradient(135deg,currentColor 50%,transparent 50%);
  background-position:calc(100% - 15px) 55%,calc(100% - 10px) 55%;background-size:5px 5px,5px 5px;background-repeat:no-repeat}
.fld:focus-visible,button:focus-visible{outline:2px solid var(--signal);outline-offset:2px}
.sw-wrap{display:flex;align-items:center;gap:.5rem}
input[type=checkbox].sw{appearance:none;-webkit-appearance:none;margin:0;position:relative;flex:0 0 auto;
  width:2.9rem;height:1.5rem;border:1px solid var(--line);border-radius:2px;background:var(--inset);cursor:pointer}
input[type=checkbox].sw::after{content:"";position:absolute;top:2px;left:2px;width:calc(1.5rem - 6px);height:calc(1.5rem - 6px);
  background:var(--muted);border-radius:1px;transition:transform .12s ease,background .12s ease}
input[type=checkbox].sw:checked{background:var(--ok-soft);border-color:var(--ok)}
input[type=checkbox].sw:checked::after{transform:translateX(1.4rem);background:var(--ok)}
@media (prefers-reduced-motion:reduce){input[type=checkbox].sw::after{transition:none}}
.sw-txt{font-family:var(--font-mono);font-size:.68rem;letter-spacing:.11em;color:var(--muted)}

/* ---- console de commande ---- */
.console{position:sticky;bottom:0;z-index:5;background:var(--panel);border-top:1px solid var(--line);
  box-shadow:0 -10px 26px -22px rgba(0,0,0,.9)}
.console .bar{height:3px;background:var(--line-soft)}
.console.busy .bar{background:repeating-linear-gradient(115deg,var(--signal) 0 12px,transparent 12px 24px);
  background-size:24px 100%;animation:slide .7s linear infinite}
@keyframes slide{to{background-position:24px 0}}
@media (prefers-reduced-motion:reduce){.console.busy .bar{animation:none}}
.console-in{max-width:74rem;margin:0 auto;padding:.8rem 1.1rem;display:flex;flex-wrap:wrap;gap:.6rem .9rem;align-items:center}
button.act{font-family:var(--font-cond);font-size:.92rem;font-weight:700;
  text-transform:uppercase;letter-spacing:.09em;padding:.6rem 1.15rem;border-radius:2px;cursor:pointer;
  border:1px solid var(--signal);background:var(--signal);color:var(--signal-ink)}
button.ghost{background:transparent;color:var(--ink);border-color:var(--line)}
button.act:disabled{opacity:.45;cursor:progress}
.log{font-family:var(--font-mono);font-size:.78rem;color:var(--muted);flex:1 1 12rem;min-width:0;overflow-wrap:anywhere}
.log.ok{color:var(--ok)} .log.ko{color:var(--stop)}
.diff{font-family:var(--font-mono);font-size:.72rem;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
.diff.on{color:var(--signal)}
.opt{display:inline-flex;align-items:center;gap:.45rem;cursor:pointer;
  font-family:var(--font-mono);font-size:.72rem;letter-spacing:.04em;color:var(--muted)}
.opt input{width:1rem;height:1rem;accent-color:var(--stop);cursor:pointer;flex:none}
.opt.on{color:var(--stop)}
footer.meta{font-family:var(--font-mono);font-size:.7rem;color:var(--muted);
  border-top:1px solid var(--line-soft);padding-top:.8rem;overflow-wrap:anywhere}
</style></head><body>

<header class="hull">
  <div class="hazard" aria-hidden="true"></div>
  <div class="hull-in">
    <div>
      <span class="eyebrow">Space Engineers · serveur dedie</span>
      <h1 id="monde">__MONDE__</h1>
      <p class="sub">Panneau local — <b class="mono" id="hote">127.0.0.1</b> · lecture et ecriture directes des fichiers du monde</p>
    </div>
    <div class="theme" role="group" aria-label="Theme de l'interface">
      <button type="button" data-m="auto" aria-pressed="true">Auto</button>
      <button type="button" data-m="light" aria-pressed="false">Clair</button>
      <button type="button" data-m="dark" aria-pressed="false">Sombre</button>
    </div>
  </div>
</header>

<div class="shell">
  <div class="lcds" id="lcds">
    <div class="lcd"><span class="k">Statut</span><div class="v">···</div><div class="n">interrogation</div></div>
    <div class="lcd"><span class="k">Sauvegarde</span><div class="v">···</div><div class="n">&nbsp;</div></div>
    <div class="lcd"><span class="k">Joueurs</span><div class="v">···</div><div class="n">&nbsp;</div></div>
  </div>

  <div class="pilote" id="pilote">
    <span class="k">Serveur</span>
    <button type="button" class="mini" id="sv_start">Demarrer</button>
    <button type="button" class="mini" id="sv_restart">Redemarrer</button>
    <button type="button" class="mini danger" id="sv_stop">Arreter</button>
    <span class="log" id="sv_msg" role="status" aria-live="polite">memes scripts que le terminal. L'arret demande au jeu de sauvegarder avant de couper.</span>
  </div>

  <div class="filtre">
    <span class="k" style="font-family:var(--font-mono);font-size:.66rem;letter-spacing:.2em;text-transform:uppercase;color:var(--muted)">Filtrer</span>
    <input id="filtre" type="search" placeholder="nom, cle XML ou texte d'aide — ex. tenseur, rotor, trash">
    <span class="n" id="filtre-n"></span>
  </div>

  <div class="notice err hide" id="err"><div id="err-txt"></div></div>

  <div class="notice"><div>Appliquer <b>arrete le serveur</b>, ecrit dans les fichiers du monde, puis le relance : les joueurs sont deconnectes. <code>stop.sh</code> demande d'abord au jeu de s'arreter proprement, ce qui <b>sauvegarde le monde</b> ; il ne coupe qu'ensuite. Le jeu met parfois jusqu'a trois minutes a repondre. S'il ne repond pas du tout, l'arret est refuse quand la derniere sauvegarde depasse <code>SE_SAVE_MAX_AGE</code>, sauf option d'arret force.</div></div>

  <form id="f" autocomplete="off"></form>

  <div id="inventaire"></div>

  <footer class="meta" id="meta"></footer>
</div>

<div class="console" id="console">
  <div class="bar" aria-hidden="true"></div>
  <div class="console-in">
    <button type="button" class="act" id="appliquer">Appliquer et redemarrer</button>
    <button type="button" class="act ghost" id="recharger">Recharger</button>
    <label class="opt" id="opt-force" title="Ne sert que si le jeu ignore la demande d'arret propre. Dans ce cas stop.sh refuse de couper quand la derniere sauvegarde est plus vieille que SE_SAVE_MAX_AGE ; cocher passe outre, et tout ce qui a ete construit depuis est perdu.">
      <input type="checkbox" id="forcer"><span>si l'arret propre echoue, couper quand meme</span>
    </label>
    <span class="diff" id="diff">aucune modification</span>
    <span class="log" id="msg" role="status" aria-live="polite"></span>
  </div>
</div>

<script>
var SCHEMA=[],VALS={},RISQUES={},chrono=null;
var MONDE_NOM="__MONDE__";

function esc(s){return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");}
function el(id){return document.getElementById(id);}

/* --- theme --- */
function theme(m){
  if(m==="auto")document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme",m);
  var b=document.querySelectorAll(".theme button");
  for(var i=0;i<b.length;i++)b[i].setAttribute("aria-pressed",String(b[i].dataset.m===m));
  try{localStorage.setItem("se-theme",m);}catch(e){}
}
(function(){
  var m="auto";try{m=localStorage.getItem("se-theme")||"auto";}catch(e){}
  theme(m);
  var b=document.querySelectorAll(".theme button");
  for(var i=0;i<b.length;i++)b[i].onclick=function(){theme(this.dataset.m);};
})();

/* --- tableau de bord --- */
function tuile(cls,k,v,u,n){
  return '<div class="lcd '+cls+'"><span class="k">'+esc(k)+'</span>'+
         '<div class="v">'+v+(u?'<span class="u">'+esc(u)+'</span>':'')+'</div>'+
         '<div class="n">'+esc(n)+'</div></div>';
}
var ETAT={};
function bord(r){
  ETAT=r||{};
  var t="";
  t+=tuile(r.enLigne?"s-ok":"s-stop","Statut",
      '<span class="dot'+(r.enLigne?" pulse":"")+'"></span>'+(r.enLigne?"En ligne":"Arrete"),"",
      r.enLigne?"processus SpaceEngineersDedicated actif":"aucun processus serveur");
  var a=r.ageSauvegarde;
  if(a<0) t+=tuile("s-stop","Sauvegarde","—","","SANDBOX_0_0_0_.sbs introuvable");
  else t+=tuile(a<=10?"s-ok":(a<=30?"s-warn":"s-stop"),"Sauvegarde",String(a),a>1?"min":"min",
      a<=10?"point de retour recent":"pense a declencher une sauvegarde");
  var j=r.joueurs,mx=r.joueursMax;
  if(j===null||j===undefined) t+=tuile("","Joueurs","n/d","","journal du serveur illisible");
  else t+=tuile(j>0?"s-ok":"","Joueurs",String(j),mx?("/ "+mx):"",
      r.enLigne?"releve d'apres le journal":"serveur arrete");
  el("lcds").innerHTML=t;
  var on=!!r.enLigne;
  el("sv_start").disabled=on;
  el("sv_stop").disabled=!on;
}
// Couper avec des joueurs connectes n'est pas anodin : le jeu ignore parfois
// la demande d'arret propre dans ce cas, et stop.sh attend alors la prochaine
// sauvegarde automatique, ce qui prend des minutes. Le dire AVANT, et demander
// un second clic, plutot que de laisser croire a un blocage.
var ARME=null;
function serveur(action,libelle){
  // Un compteur inconnu (null) n'est PAS zero : c'est le cas juste apres un
  // demarrage, avant la premiere ligne de statistiques du journal. Le traiter
  // comme zero ferait sauter l'avertissement precisement quand on ne sait pas.
  var j=ETAT.joueurs;
  var risque=(action!=="demarrer")&&ETAT.enLigne&&(j===null||j===undefined||j>0);
  if(risque&&ARME!==action){
    ARME=action;
    el("sv_msg").className="log ko";
    el("sv_msg").textContent=(j?(j+" joueur"+(j>1?"s":"")+" connecte"+(j>1?"s":""))
                                :"nombre de joueurs inconnu")+
      ". Le jeu peut mettre plusieurs minutes a repondre ; l arret attendra une "+
      "sauvegarde avant de couper. Recliquer pour confirmer.";
    return;
  }
  ARME=null;
  var b=document.querySelectorAll("#pilote .mini");
  b.forEach(function(x){x.disabled=true;});
  el("console").classList.add("busy");
  var t0=Date.now();
  el("sv_msg").className="log";el("sv_msg").textContent=libelle+" — 0 s";
  var tick=setInterval(function(){
    el("sv_msg").textContent=libelle+" — "+Math.round((Date.now()-t0)/1000)+" s";},1000);
  function fini(){clearInterval(tick);el("console").classList.remove("busy");
    b.forEach(function(x){x.disabled=false;});charger();inventaire();}
  fetch("/api/serveur",{method:"POST",headers:{"content-type":"application/json"},
    body:JSON.stringify({action:action,forcer:!!(el("forcer")&&el("forcer").checked)})})
   .then(function(x){return x.json();}).then(function(r){
      el("sv_msg").className="log "+(r.ok?"ok":"ko");
      el("sv_msg").textContent=r.ok?(libelle+" : "+(r.note||"fait"))
        :((r.arretRefuse?"ARRET REFUSE  —  ":"echec  —  ")+(r.erreur||"inconnue"));
      fini();
   }).catch(function(e){el("sv_msg").className="log ko";
      el("sv_msg").textContent="echec reseau : "+e;fini();});
}
el("sv_start").onclick=function(){serveur("demarrer","demarrage");};
el("sv_restart").onclick=function(){serveur("redemarrer","redemarrage");};
el("sv_stop").onclick=function(){serveur("arreter","arret");};

/* --- formulaire --- */
function champ(cle,type,choix,v){
  var id="f_"+cle;
  if(type==="bool"){
    var on=(v==="true");
    return '<div class="sw-wrap"><input type="checkbox" class="sw" id="'+id+'" data-k="'+esc(cle)+'"'+(on?" checked":"")+
           '><span class="sw-txt">'+(on?"ACTIF":"INACTIF")+'</span></div>';
  }
  if(type==="choix"){
    var o="";
    for(var i=0;i<choix.length;i++)o+='<option'+(choix[i]===v?" selected":"")+'>'+esc(choix[i])+'</option>';
    return '<select class="fld" id="'+id+'" data-k="'+esc(cle)+'">'+o+'</select>';
  }
  return '<input type="text" inputmode="decimal" class="fld" id="'+id+'" data-k="'+esc(cle)+'" value="'+esc(v)+'">';
}
function rendre(){
  var f=el("f");f.innerHTML="";
  for(var i=0;i<SCHEMA.length;i++){
    var groupe=SCHEMA[i][0],items=SCHEMA[i][1],corps="",n=0;
    for(var j=0;j<items.length;j++){
      var cle=items[j][0],lib=items[j][1],type=items[j][2],aide=items[j][3],choix=items[j][4];
      var v=VALS[cle];if(v===undefined)continue;
      n++;
      var niveau=RISQUES[cle]||"";
      var badge=niveau==="critique"?'<span class="tag t-crit">critique</span>':
                (niveau==="attention"?'<span class="tag t-warn">attention</span>':"");
      corps+='<div class="row'+(niveau?" r-"+niveau:"")+'" data-row="'+esc(cle)+'">'+
             '<div class="row-main"><div class="row-top"><label class="nom" for="f_'+esc(cle)+'">'+esc(lib)+'</label>'+badge+'</div>'+
             '<code class="cle">'+esc(cle)+'</code>'+(aide?'<p class="aide">'+esc(aide)+'</p>':'')+'</div>'+
             '<div>'+champ(cle,type,choix,v)+'</div></div>';
    }
    if(!n)continue;
    var s=document.createElement("section");
    s.innerHTML='<div class="sec-head"><span class="sec-num">'+("0"+(i+1)).slice(-2)+'</span>'+
                '<h2>'+esc(groupe)+'</h2><span class="sec-rule"></span><span class="sec-count">'+n+' reglages</span></div>'+
                '<div class="grid">'+corps+'</div>';
    f.appendChild(s);
  }
  filtrer();
  diff();
}
// A 190 reglages, parcourir la page ne marche plus. Le filtre cherche dans le
// libelle, la cle XML et l'aide, et masque les sections devenues vides plutot
// que de laisser des titres orphelins.
function filtrer(){
  var q=(el("filtre").value||"").trim().toLowerCase();
  var lignes=document.querySelectorAll("#f .row"),vus=0;
  for(var i=0;i<lignes.length;i++){
    var r=lignes[i];
    var ok=!q||(r.textContent||"").toLowerCase().indexOf(q)>=0;
    r.classList.toggle("hide",!ok);
    if(ok)vus++;
  }
  var secs=document.querySelectorAll("#f section");
  for(var s=0;s<secs.length;s++){
    secs[s].classList.toggle("hide",!secs[s].querySelector(".row:not(.hide)"));
  }
  el("filtre-n").textContent=q?(vus+" / "+lignes.length):(lignes.length+" reglages");
}
el("filtre").oninput=filtrer;
function courant(e){return e.type==="checkbox"?String(e.checked):e.value.trim();}
function diff(){
  var ch=document.querySelectorAll("[data-k]"),n=0;
  for(var i=0;i<ch.length;i++){
    var e=ch[i],r=document.querySelector('[data-row="'+e.dataset.k+'"]');
    var change=courant(e)!==String(VALS[e.dataset.k]);
    if(r)r.classList.toggle("mod",change);
    if(change)n++;
  }
  var d=el("diff");
  d.textContent=n?(n+" modification"+(n>1?"s":"")+" en attente"):"aucune modification";
  d.classList.toggle("on",n>0);
}
el("f").addEventListener("input",function(ev){
  var e=ev.target;
  if(e.type==="checkbox"){
    var t=e.parentNode.querySelector(".sw-txt");
    if(t)t.textContent=e.checked?"ACTIF":"INACTIF";
  }
  diff();
});

/* --- reseau : GET /api/etat, POST /api/appliquer.
       Corps du POST : {valeurs:{cle:valeur}, forcer:bool}. "forcer" ajoute
       --force a stop.sh et n'est jamais implicite. --- */
async function charger(){
  try{
    var r=await(await fetch("/api/etat")).json();
    if(!r.schema){
      el("err").classList.remove("hide");
      el("err-txt").textContent=r.erreur||"Reponse inattendue du panneau.";
      return;
    }
    SCHEMA=r.schema;VALS=r.valeurs;RISQUES=r.risques||{};
    if(r.monde){el("monde").textContent=r.monde;document.title="Reglages — "+r.monde;}
    bord(r);rendre();
    el("err").classList.toggle("hide",!r.erreur);
    if(r.erreur)el("err-txt").textContent=r.erreur;
    el("meta").textContent="monde "+(r.monde||"?")+" · "+Object.keys(VALS).length+" valeurs lues · "+new Date().toLocaleTimeString();
  }catch(e){
    el("err").classList.remove("hide");el("err-txt").textContent="Panneau injoignable : "+e;
  }
}
el("hote").textContent=location.host;
function secHead(num,titre,compte){
  return "<div class=\"sec-head\"><span class=\"sec-num\">"+num+"</span><h2>"+esc(titre)+
         "</h2><span class=\"sec-rule\"></span><span class=\"sec-count\">"+esc(compte)+"</span></div>";
}
function invAction(n,forcer){
  var b=document.querySelector("[data-conf=\""+CSS.escape(n)+"\"]");
  if(b)b.textContent="EN COURS";
  document.querySelectorAll("#inventaire .mini").forEach(function(x){x.disabled=true;});
  fetch("/api/restaurer",{method:"POST",headers:{"content-type":"application/json"},
    body:JSON.stringify({sauvegarde:n,forcer:!!forcer})})
   .then(function(x){return x.json();}).then(function(r){
      var m=el("msg");
      if(r.ok){m.textContent="monde restaure depuis "+n+", etat precedent garde dans "+r.filet;}
      else{m.textContent=(r.arretRefuse?"ARRET REFUSE, le monde n a pas ete touche  —  ":"echec  —  ")+(r.erreur||"");}
      charger();inventaire();
   }).catch(function(){el("msg").textContent="echec reseau";inventaire();});
}
function invOuvrir(n,quoi){
  fetch("/api/reveler",{method:"POST",headers:{"content-type":"application/json"},
    body:JSON.stringify({sauvegarde:n,quoi:quoi||null})}).then(function(x){return x.json();}).then(function(r){
      if(!r.ok)el("msg").textContent="ouverture impossible  —  "+(r.erreur||"");
   }).catch(function(){});
}
function modConfirmer(id){
  var c=document.querySelector("[data-modact=\""+CSS.escape(id)+"\"]");
  if(!c)return;
  c.innerHTML='<button type="button" class="mini" data-annule="1">Annuler</button>'+
              '<button type="button" class="mini danger" data-modok="'+esc(id)+'">Confirmer</button>';
  c.querySelector("[data-annule]").onclick=function(){inventaire();};
  c.querySelector("[data-modok]").onclick=function(){admAction({action:"mod_retirer",mod:id},"retrait du mod");};
}
function invConfirmer(n){
  var c=document.querySelector("[data-act=\""+CSS.escape(n)+"\"]");
  if(!c)return;
  var f=el("forcer")&&el("forcer").checked;
  c.innerHTML='<button type="button" class="mini" data-annule="1">Annuler</button>'+
              '<button type="button" class="mini danger" data-conf="'+esc(n)+'">Confirmer</button>';
  c.querySelector("[data-annule]").onclick=function(){inventaire();};
  c.querySelector("[data-conf]").onclick=function(){invAction(n,f);};
}
function admAction(corps,libelle){
  document.querySelectorAll("#inventaire .mini,#inventaire input").forEach(function(x){x.disabled=true;});
  el("msg").textContent=libelle+" en cours, le serveur redemarre...";
  corps.forcer=!!(el("forcer")&&el("forcer").checked);
  fetch("/api/administration",{method:"POST",headers:{"content-type":"application/json"},
    body:JSON.stringify(corps)}).then(function(x){return x.json();}).then(function(r){
      el("msg").textContent=r.ok?(libelle+" applique : "+(r.resume||[]).join(", "))
        :((r.arretRefuse?"ARRET REFUSE, rien n a ete ecrit  —  ":"echec  —  ")+(r.erreur||""));
      charger();inventaire();
    }).catch(function(){el("msg").textContent="echec reseau";inventaire();});
}
function rendreAdmin(a){
  var id=a.identite||{},js=a.joueurs||[],h="";
  h+="<section>"+secHead("S1","Serveur","identite et acces")+"<div class=\"tbl\" style=\"padding:.2rem 0\">"+
     "<div class=\"champ-ligne\"><label for=\"a_sn\">Nom du serveur</label>"+
     "<input id=\"a_sn\" type=\"text\" value=\""+esc(id.ServerName||"")+"\">"+
     "<button type=\"button\" class=\"mini\" id=\"a_sn_b\">Enregistrer</button>"+
     "<code class=\"cle\">ServerName  —  ce que voient les joueurs dans la liste des serveurs</code></div>"+
     "<div class=\"champ-ligne\"><label for=\"a_wn\">Nom affiche du monde</label>"+
     "<input id=\"a_wn\" type=\"text\" value=\""+esc(id.WorldName||"")+"\">"+
     "<button type=\"button\" class=\"mini\" id=\"a_wn_b\">Enregistrer</button>"+
     "<code class=\"cle\">WorldName  —  n est pas le nom du DOSSIER du monde, qui reste "+esc(MONDE_NOM)+"</code></div>"+
     "<div class=\"champ-ligne\"><label for=\"a_mp\">Mot de passe</label>"+
     "<input id=\"a_mp\" type=\"password\" autocomplete=\"new-password\" placeholder=\""+
     (id.motDePasse?"un mot de passe est defini":"aucun mot de passe")+"\">"+
     "<button type=\"button\" class=\"mini\" id=\"a_mp_b\">Changer</button>"+
     "<button type=\"button\" class=\"mini danger\" id=\"a_mp_r\">Retirer</button>"+
     "<code class=\"cle\">calcule en local, PBKDF2-HMAC-SHA1 ; il n est jamais stocke ni envoye en clair</code></div>"+
     "</div></section>";

  h+="<section>"+secHead("S2","Joueurs",js.length+" connus du monde")+"<div class=\"wrap\"><table class=\"tbl\">"+
     "<tr><th></th><th>Joueur</th><th>Identifiant</th><th></th></tr>";
  for(var i=0;i<js.length;i++){
    var j=js[i];
    var p=(j.niveau==="Admin"?"<span class=\"pastille admin\">admin</span> ":"")+
          (j.banni?"<span class=\"pastille banni\">banni</span> ":"");
    h+="<tr><td class=\"num\">"+(i+1)+"</td><td>"+p+esc(j.nom)+"</td>"+
       "<td class=\"droite\">"+esc(j.hashedId)+"</td><td><div class=\"actions\">"+
       "<button type=\"button\" class=\"mini\" data-prom=\""+esc(j.hashedId)+"\" data-niv=\""+
       (j.niveau==="Admin"?"None":"Admin")+"\">"+(j.niveau==="Admin"?"Retirer admin":"Passer admin")+"</button>"+
       "<button type=\"button\" class=\"mini"+(j.banni?"":" danger")+"\" data-ban=\""+esc(j.hashedId)+"\" data-etat=\""+
       (j.banni?"0":"1")+"\">"+(j.banni?"Debannir":"Bannir")+"</button>"+
       "</div></td></tr>";
  }
  if(!js.length)h+="<tr><td colspan=\"4\" class=\"sansnom\">Aucun joueur connu. Un joueur apparait apres sa premiere connexion.</td></tr>";
  h+="</table></div><p class=\"aide\">Chaque action arrete le serveur, ecrit, puis relance. "+
     "Les promotions vivent dans la sauvegarde et sont propres a CE monde : changer de monde les remet a zero. "+
     "Le bannissement vit dans la configuration du serveur et vaut pour tous les mondes.</p></section>";
  return h;
}
function inventaire(){
  Promise.all([fetch("/api/administration").then(function(x){return x.json();}),
               fetch("/api/inventaire").then(function(x){return x.json();})])
  .then(function(res){
    var a=res[0]||{},r=res[1]||{};
    var h=rendreAdmin(a);
    var s=r.sauvegardes||{},m=r.mods||{},lignes=s.liste||[];
    h+="<section>"+secHead("S3","Sauvegardes",(s.nombre||0)+" au total, "+(s.total||"0 o"))+
       "<div class=\"wrap\"><table class=\"tbl\">"+
       "<tr><th></th><th>Horodatage</th><th class=\"droite\">Fichiers</th><th class=\"droite\">Taille</th><th></th></tr>";
    if(s.vif){
      h+="<tr><td class=\"num\">&bull;</td><td><b>Monde en cours</b> <span class=\"sansnom\">ecrit il y a "+
         (s.vifAge<0?"?":s.vifAge)+" min</span></td><td class=\"droite\">&mdash;</td><td class=\"droite\">"+esc(s.vif)+
         "</td><td><div class=\"actions\"><button type=\"button\" class=\"mini\" data-vif=\"1\">Ouvrir</button></div></td></tr>";
    }
    for(var i=0;i<lignes.length;i++){
      var n=lignes[i].nom;
      h+="<tr"+(i===0?" class=\"frais\"":"")+"><td class=\"num\">"+(i+1)+"</td><td class=\"mono\">"+esc(lignes[i].quand)+
         "</td><td class=\"droite\">"+lignes[i].fichiers+"</td><td class=\"droite\">"+esc(lignes[i].taille)+
         "</td><td><div class=\"actions\" data-act=\""+esc(n)+"\">"+
         "<button type=\"button\" class=\"mini\" data-ouvre=\""+esc(n)+"\">Ouvrir</button>"+
         "<button type=\"button\" class=\"mini danger\" data-rest=\""+esc(n)+"\">Restaurer</button>"+
         "</div></td></tr>";
    }
    if(!lignes.length)h+="<tr><td colspan=\"5\" class=\"sansnom\">Aucune sauvegarde automatique pour le moment.</td></tr>";
    h+="</table></div><p class=\"aide\">Restaurer arrete le serveur, met le monde actuel de cote dans Backup/ sous un nom "+
       "horodate, remet la sauvegarde choisie, puis relance. Une restauration se defait donc par une autre restauration.</p></section>";

    var lm=m.liste||[],dm=m.dormants||[];
    var note=m.sansNom?((m.nombre||0)+" actifs, "+m.sansNom+" sans nom connu"):((m.nombre||0)+" actifs");
    h+="<section>"+secHead("S4","Mods",note)+"<div class=\"wrap\"><table class=\"tbl\">"+
       "<tr><th></th><th>Nom</th><th>Identifiant</th><th class=\"droite\">Archive</th><th></th></tr>";
    for(var k=0;k<lm.length;k++){
      var md=lm[k];
      var nn=md.nom?esc(md.nom):"<span class=\"sansnom\">nom inconnu, le serveur ne l a pas encore journalise</span>";
      h+="<tr><td class=\"num\">"+(k+1)+"</td><td>"+
         (md.dependance?"<span class=\"pastille dep\">dependance</span> ":"")+nn+"</td>"+
         "<td class=\"droite\"><a href=\""+esc(md.lien)+"\" target=\"_blank\" rel=\"noopener\">"+esc(md.id)+
         "</a> <span class=\"sansnom\">"+esc(md.service||"")+"</span></td>"+
         "<td class=\"droite\">"+(md.cache?esc(md.cache):"<span class=\"sansnom\">a telecharger</span>")+"</td>"+
         "<td><div class=\"actions\" data-modact=\""+esc(md.id)+"\">"+
         "<button type=\"button\" class=\"mini danger\" data-modret=\""+esc(md.id)+"\">Retirer</button>"+
         "</div></td></tr>";
    }
    if(!lm.length)h+="<tr><td colspan=\"5\" class=\"sansnom\">Aucun mod dans ce monde.</td></tr>";
    h+="</table></div>";

    h+="<div class=\"tbl\" style=\"margin-top:.7rem;padding:.2rem 0\">"+
       "<div class=\"champ-ligne\"><label for=\"m_id\">Ajouter un mod</label>"+
       "<input id=\"m_id\" type=\"text\" inputmode=\"numeric\" placeholder=\"numero mod.io, ex. 750855\">"+
       "<input id=\"m_nom\" type=\"text\" placeholder=\"nom, facultatif\">"+
       "<button type=\"button\" class=\"mini\" id=\"m_add\">Ajouter</button>"+
       "<code class=\"cle\">le numero est la fin de l URL mod.io  —  ajoute en dernier, donc prioritaire en cas de conflit</code></div>"+
       "<div class=\"champ-ligne\"><label>Dossier des mods</label>"+
       "<button type=\"button\" class=\"mini\" id=\"m_cache\">Ouvrir le cache</button>"+
       "<code class=\"cle\">"+esc(m.cacheChemin||"")+"  —  "+esc(m.cacheTotal||"0 o")+
       " au total. Mods/ reste vide sur un serveur dedie, tout est ici, sous le seul numero.</code></div></div>";

    h+="<div class=\"wrap\" style=\"margin-top:.7rem\"><table class=\"tbl\">"+
       "<tr><th></th><th>Telecharge mais pas charge</th><th class=\"droite\">"+esc(m.dormantsPoids||"0 o")+"</th><th></th></tr>";
    for(var d=0;d<dm.length;d++){
      var dd=dm[d];
      var dn=dd.nom?esc(dd.nom):"<span class=\"sansnom\">nom inconnu</span>";
      h+="<tr><td class=\"num\">"+(d+1)+"</td><td>"+dn+" <a href=\""+esc(dd.lien)+
         "\" target=\"_blank\" rel=\"noopener\" class=\"sansnom\">"+esc(dd.id)+"</a></td>"+
         "<td class=\"droite\">"+esc(dd.taille)+"</td>"+
         "<td><div class=\"actions\"><button type=\"button\" class=\"mini\" data-modadd=\""+esc(dd.id)+
         "\" data-modnom=\""+esc(dd.nom||"")+"\">Remettre</button></div></td></tr>";
    }
    if(!dm.length)h+="<tr><td colspan=\"4\" class=\"sansnom\">Le cache ne contient que les mods du monde.</td></tr>";
    h+="</table></div>";

    h+="<p class=\"aide\">Ordre de chargement : Space Engineers applique les mods de haut en bas, le dernier gagne "+
       "en cas de conflit. Ajouter ou retirer arrete le serveur, ecrit les TROIS fichiers qui portent la liste "+
       "(le .cfg n a pas les dependances resolues par le jeu), puis relance. Retirer ne supprime pas l archive : "+
       "elle reste dans le cache, donc le mod revient sans retelechargement. Une entree marquee dependance a ete "+
       "tiree par un autre mod : la retirer seule et le jeu la remettra au demarrage suivant. "+
       "Les noms viennent du fichier du monde, pas du reseau.</p></section>";
    el("inventaire").innerHTML=h;

    var inv=el("inventaire");
    inv.querySelectorAll("[data-ouvre]").forEach(function(b){b.onclick=function(){invOuvrir(b.dataset.ouvre);};});
    inv.querySelectorAll("[data-rest]").forEach(function(b){b.onclick=function(){invConfirmer(b.dataset.rest);};});
    var v=inv.querySelector("[data-vif]");if(v)v.onclick=function(){invOuvrir("");};
    inv.querySelectorAll("[data-modret]").forEach(function(b){b.onclick=function(){modConfirmer(b.dataset.modret);};});
    inv.querySelectorAll("[data-modadd]").forEach(function(b){b.onclick=function(){
      admAction({action:"mod_ajouter",mod:b.dataset.modadd,nomMod:b.dataset.modnom||""},"ajout du mod");};});
    el("m_cache").onclick=function(){invOuvrir("","cache");};
    el("m_add").onclick=function(){
      var v=(el("m_id").value||"").trim().replace(/^.*[\\/=]/,"");
      if(!/^[0-9]{1,12}$/.test(v)){el("msg").textContent="saisis le numero du mod, des chiffres uniquement";return;}
      admAction({action:"mod_ajouter",mod:v,nomMod:(el("m_nom").value||"").trim()},"ajout du mod");};
    inv.querySelectorAll("[data-prom]").forEach(function(b){b.onclick=function(){
      admAction({action:"promouvoir",hashedId:b.dataset.prom,niveau:b.dataset.niv},"promotion");};});
    inv.querySelectorAll("[data-ban]").forEach(function(b){b.onclick=function(){
      admAction({action:"bannir",hashedId:b.dataset.ban,banni:b.dataset.etat==="1"},
                b.dataset.etat==="1"?"bannissement":"debannissement");};});
    el("a_sn_b").onclick=function(){admAction({action:"identite",ServerName:el("a_sn").value.trim()},"nom du serveur");};
    el("a_wn_b").onclick=function(){admAction({action:"identite",WorldName:el("a_wn").value.trim()},"nom du monde");};
    el("a_mp_b").onclick=function(){
      var v=el("a_mp").value;
      if(!v){el("msg").textContent="saisis un mot de passe avant de valider";return;}
      admAction({action:"motdepasse",motDePasse:v},"mot de passe");};
    el("a_mp_r").onclick=function(){admAction({action:"motdepasse",motDePasse:""},"retrait du mot de passe");};
  }).catch(function(){});
}
el("recharger").onclick=function(){charger();inventaire();};
el("forcer").onchange=function(){el("opt-force").classList.toggle("on",this.checked);};
el("appliquer").onclick=async function(){
  var b=el("appliquer"),m=el("msg"),c=el("console"),out={};
  var ch=document.querySelectorAll("[data-k]");
  for(var i=0;i<ch.length;i++)out[ch[i].dataset.k]=courant(ch[i]);
  b.disabled=true;c.classList.add("busy");m.className="log";
  var t0=Date.now();
  clearInterval(chrono);
  chrono=setInterval(function(){
    m.textContent="arret, ecriture, redemarrage — "+Math.round((Date.now()-t0)/1000)+" s (compter plusieurs minutes)";
  },1000);
  m.textContent="arret, ecriture, redemarrage — 0 s (compter plusieurs minutes)";
  try{
    var r=await(await fetch("/api/appliquer",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({valeurs:out,forcer:el("forcer").checked})})).json();
    clearInterval(chrono);
    m.className="log "+(r.ok?"ok":"ko");
    m.textContent=r.ok?("applique, serveur relance"+(r.note?" — "+r.note:""))
                      :("echec : "+(r.erreur||"inconnue"));
  }catch(e){
    clearInterval(chrono);m.className="log ko";m.textContent="erreur : "+e;
  }
  b.disabled=false;c.classList.remove("busy");charger();
};
charger();
inventaire();
</script></body></html>"""


def page():
    return PAGE.replace("__MONDE__", html.escape(MONDE))


class H(http.server.BaseHTTPRequestHandler):
    server_version = "SEPanel/1.0"

    def log_message(self, *a):
        # Les GET sont interroges en boucle par la page : les journaliser
        # noierait le seul evenement qui compte. Les POST le sont dans do_POST,
        # avec l'action demandee.
        pass

    def _trace(self, quoi):
        """Trace une action qui touche au serveur.

        Sans elle le panneau est une boite noire : quand le serveur s'arrete,
        rien ne permet de dire si une requete l'a demande ou si le jeu est
        tombe seul. C'est exactement la question qui s'est posee le 22/08.

        Sur la sortie d'erreur ET dans un fichier : la sortie d'erreur part
        avec la fenetre qui a lance le panneau, et c'est justement plusieurs
        heures apres qu'on vient chercher la reponse.
        """
        ligne = "%s  %s  depuis %s" % (
            datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            quoi, self.client_address[0])
        print(ligne, file=sys.stderr, flush=True)
        try:
            journal = ROOT / "logs"
            journal.mkdir(exist_ok=True)
            with (journal / "panel.log").open("a", encoding="utf-8") as f:
                f.write(ligne + "\n")
        except OSError:
            pass                                # tracer ne doit jamais bloquer

    def _j(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _garde(self, json_requis=False):
        """Refuse ce qui n'a pas ete emis depuis cette machine.

        Renvoie True si la requete peut continuer, sinon repond et renvoie
        False.
        """
        hote = self.headers.get("Host")
        if not _hote_ok(hote):
            self._j({"ok": False, "erreur": "Host refuse : ce panneau ne repond qu'a sa propre adresse."}, 403)
            return False
        if not _origine_ok(self.headers.get("Origin"), hote):
            self._j({"ok": False, "erreur": "Origin refuse : requete declenchee par une autre page."}, 403)
            return False
        if json_requis:
            ct = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ct != "application/json":
                self._j({"ok": False, "erreur": "Content-Type attendu : application/json."}, 415)
                return False
        return True

    def do_GET(self):
        if not self._garde():
            return
        if self.path.startswith("/api/administration"):
            return self._j(administration())
        if self.path.startswith("/api/inventaire"):
            return self._j({"sauvegardes": sauvegardes(), "mods": mods()})
        if self.path.startswith("/api/etat"):
            valeurs = lire()
            return self._j({**etat(valeurs), "valeurs": valeurs, "risques": RISQUES,
                            "schema": [[g, [list(x) for x in items]] for g, items in REGLAGES]})
        b = page().encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        if not (self.path.startswith("/api/appliquer")
                or self.path.startswith("/api/serveur")
                or self.path.startswith("/api/restaurer")
                or self.path.startswith("/api/reveler")
                or self.path.startswith("/api/administration")):
            return self._j({"ok": False}, 404)
        if not self._garde(json_requis=True):
            return
        taille = _entier(self.headers.get("Content-Length"), -1)
        if taille < 0 or taille > 1 << 20:
            return self._j({"ok": False, "erreur": "Content-Length absent ou hors bornes."}, 400)
        try:
            recu = json.loads(self.rfile.read(taille) or b"{}")
        except ValueError as e:
            return self._j({"ok": False, "erreur": "JSON illisible : " + str(e)})
        if not isinstance(recu, dict):
            return self._j({"ok": False, "erreur": "JSON attendu : un objet."})

        if self.path.startswith("/api/administration"):
            act = recu.get("action")
            self._trace("administration %s (mod=%s, joueur=%s)"
                        % (act, recu.get("mod"), recu.get("hashedId")))
            return self._j(appliquer_administration(
                act, bool(recu.get("forcer")),
                ServerName=recu.get("ServerName"), WorldName=recu.get("WorldName"),
                motDePasse=recu.get("motDePasse"), hashedId=recu.get("hashedId"),
                niveau=recu.get("niveau"), banni=recu.get("banni"),
                mod=recu.get("mod"), nomMod=recu.get("nomMod")))
        if self.path.startswith("/api/serveur"):
            self._trace("serveur %s (forcer=%s)" % (recu.get("action"), bool(recu.get("forcer"))))
            return self._j(commander_serveur(recu.get("action"), bool(recu.get("forcer"))))
        if self.path.startswith("/api/reveler"):
            return self._j(reveler(recu.get("sauvegarde"), recu.get("quoi")))
        if self.path.startswith("/api/restaurer"):
            self._trace("restaurer %s (forcer=%s)"
                        % (recu.get("sauvegarde"), bool(recu.get("forcer"))))
            return self._j(restaurer(recu.get("sauvegarde"), bool(recu.get("forcer"))))

        # Corps attendu : {"valeurs": {...}, "forcer": true|false}. Un objet
        # plat reste accepte et vaut forcer=false : l'arret force ne peut pas
        # etre obtenu par omission.
        vals = recu.get("valeurs")
        if isinstance(vals, dict):
            forcer = bool(recu.get("forcer"))
        else:
            vals, forcer = recu, False

        self._trace("appliquer %d reglage(s)" % (len(vals) if isinstance(vals, dict) else 0))
        souci = probleme()
        if souci:
            return self._j({"ok": False, "erreur": souci})
        propres, refuses = valider(vals)
        if refuses:
            return self._j({"ok": False, "erreur": "valeurs refusees : " + ", ".join(sorted(refuses))})
        arret, demarrage = ROOT / "scripts/stop.sh", ROOT / "scripts/start.sh"
        for script in (arret, demarrage):
            if not script.is_file():
                return self._j({"ok": False, "erreur": "script introuvable : " + str(script) + " (verifie SE_ROOT)"})
        try:
            # 1. Arreter d'abord. Editer les fichiers du monde pendant que le
            #    serveur tourne ne sert a rien : la prochaine sauvegarde auto
            #    les reecrit depuis la memoire. Et si stop.sh refuse de couper,
            #    rien ne doit avoir ete ecrit.
            #
            #    --force n'est ajoute que si l'interface l'a demande. Sans lui,
            #    le garde-fou d'age de sauvegarde de stop.sh s'applique, et un
            #    refus est un refus de perdre du travail.
            cmd = ["bash", str(arret)] + (["--force"] if forcer else [])
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=DELAI_ARRET)
            if r.returncode != 0:
                detail = " ".join(((r.stdout or "") + " " + (r.stderr or "")).split())
                return self._j({"ok": False, "arretRefuse": True,
                                "erreur": "arret refuse, rien n'a ete ecrit : " + detail[:600]})

            # 2. Ecrire les trois fichiers, puis retirer la copie compressee
            #    que le jeu relirait de preference.
            modifies = ecrire(propres)
            note = supprimer_sbsb5()

            # 3. Relancer.
            subprocess.run(["bash", str(demarrage)], capture_output=True, timeout=DELAI_DEMARRAGE)
            return self._j({"ok": etat()["enLigne"], "fichiers": modifies, "note": note})
        except Exception as e:
            return self._j({"ok": False, "erreur": str(e)})


if __name__ == "__main__":
    souci = probleme()
    if souci:
        print("ATTENTION : " + souci, file=sys.stderr)
        print("            le panneau demarre quand meme et affiche le diagnostic.", file=sys.stderr)
    if not _boucle_locale(HOTE) and not AUTORISE_DISTANT:
        print("REFUS : SE_PANEL_HOST=%s n'est pas une adresse de boucle locale." % HOTE, file=sys.stderr)
        print("        Le panneau n'a aucune authentification et sait redemarrer le", file=sys.stderr)
        print("        serveur : l'ouvrir sur un reseau donne ce pouvoir a tout le", file=sys.stderr)
        print("        monde. Prefere un tunnel SSH :", file=sys.stderr)
        print("            ssh -N -L %d:127.0.0.1:%d utilisateur@le-mac" % (PORT, PORT), file=sys.stderr)
        print("        Si c'est bien ce que tu veux : SE_PANEL_ALLOW_REMOTE=1.", file=sys.stderr)
        sys.exit(2)
    socketserver.TCPServer.allow_reuse_address = True
    try:
        serveur = socketserver.TCPServer((HOTE, PORT), H)
    except OSError as e:
        # Cas courant : un panneau tourne deja, oublie dans un autre terminal.
        # La trace brute d'un "Address already in use" ne le dit pas, et laisse
        # croire que le port est casse alors qu'il suffit d'ouvrir la page.
        if e.errno not in (48, 98):                     # EADDRINUSE, BSD et Linux
            raise
        print("Le port %d est deja pris." % PORT, file=sys.stderr)
        deja = None
        try:
            deja = subprocess.run(["lsof", "-nP", "-tiTCP:%d" % PORT, "-sTCP:LISTEN"],
                                  capture_output=True, text=True, timeout=10).stdout.split()
        except (OSError, subprocess.SubprocessError):
            pass
        if deja:
            comm = subprocess.run(["ps", "-o", "comm=", "-p", deja[0]],
                                  capture_output=True, text=True).stdout.strip()
            print("        PID %s (%s)" % (deja[0], comm or "?"), file=sys.stderr)
        print("        Si c'est un panneau oublie, il sert deja http://%s:%d" % (HOTE, PORT),
              file=sys.stderr)
        print("        Sinon :  kill %s   ou   SE_PANEL_PORT=%d python3 panel/settings.py"
              % (deja[0] if deja else "<pid>", PORT + 1), file=sys.stderr)
        sys.exit(3)

    with serveur as s:
        if not _boucle_locale(HOTE):
            print("ATTENTION : ecoute sur %s, sans authentification." % HOTE, file=sys.stderr)
        print("Panneau de reglages : http://%s:%d" % (HOTE, PORT))
        print("Monde : %s   Racine : %s" % (MONDE, ROOT))
        print("Ctrl+C pour arreter.")
        try:
            s.serve_forever()
        except KeyboardInterrupt:
            print("")
