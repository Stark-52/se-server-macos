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
import getpass
import html
import http.server
import ipaddress
import json
import os
import pathlib
import re
import socketserver
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

# --------------------------------------------------------------------------
# Schema des reglages : cle XML, libelle, type, aide, [choix]
# --------------------------------------------------------------------------
REGLAGES = [
 ("Rendement", [
  ("InventorySizeMultiplier",      "Taille des inventaires", "num", "Multiplie la capacite du perso et des conteneurs. 1 = vanilla, 10 = confortable."),
  ("AssemblerSpeedMultiplier",     "Vitesse assembleurs",    "num", "Rapidite de fabrication."),
  ("AssemblerEfficiencyMultiplier","Rendement assembleurs",  "num", "Moins de minerai par composant."),
  ("RefinerySpeedMultiplier",      "Vitesse raffineries",    "num", "Rapidite de raffinage."),
  ("WelderSpeedMultiplier",        "Vitesse soudeuses",      "num", "Rapidite de construction."),
  ("GrinderSpeedMultiplier",       "Vitesse meuleuses",      "num", "Rapidite de demontage."),
  ("HackSpeedMultiplier",          "Vitesse de piratage",    "num", "Prise de controle des blocs ennemis. Bas = long."),
 ]),
 ("Survie", [
  ("FoodConsumptionRate",  "Vitesse de la faim",       "num",  "0 = desactive. 0.5 = ce que livre Keen. Plus haut = on meurt plus vite."),
  ("EnableSurvivalBuffs",  "Barre de nourriture",      "bool", "Systeme de faim et de buffs de la MAJ Apex Survival."),
  ("AutoHealing",          "Regeneration auto",        "bool", "Le perso se soigne seul dans un cockpit pressurise."),
  ("EnableOxygen",         "Oxygene",                  "bool", "Gestion de l'oxygene."),
  ("EnableRespawnShips",   "Vaisseaux de secours",     "bool", "Reapparition avec un vaisseau. A garder actif."),
  ("PermanentDeath",       "Mort definitive",          "bool", "Perte du personnage a la mort. Brutal."),
 ]),
 ("Monde", [
  ("MaxPlayers",            "Joueurs maximum",         "num",  ""),
  ("AutoSaveInMinutes",     "Sauvegarde auto (min)",   "num",  "Ce qui n'est pas sauvegarde est perdu a l'arret du serveur."),
  ("EnvironmentHostility",  "Hostilite",               "choix","Meteorites. CATACLYSM et ARMAGEDDON rasent les bases.",
     ["SAFE","NORMAL","CATACLYSM","ARMAGEDDON"]),
  ("EnableEconomy",         "Economie",                "bool", "Stations commerciales et contrats."),
  ("EnableIngameScripts",   "Scripts in-game",         "bool", "Bloc programmable. Force le mode Experimental."),
  ("EnableDrones",          "Drones",                  "bool", ""),
  ("EnableSpiders",         "Araignees",               "bool", "Un mod de rencontres peut reprendre ce reglage a son compte."),
  ("EnableWolfs",           "Loups",                   "bool", "Un mod de rencontres peut reprendre ce reglage a son compte."),
  ("EnableCopyPaste",       "Copier-coller",           "bool", "Mode creatif. A laisser desactive en survie."),
 ]),
 ("Limites", [
  ("TotalPCU",           "PCU total",           "num",  "Budget de blocs du monde. A monter pour les tres grosses constructions."),
  ("MaxGridSize",        "Taille max d'une grille","num","0 = illimite."),
  ("BlockLimitsEnabled", "Limites de blocs",    "choix","", ["NONE","GLOBALLY","PER_PLAYER","PER_FACTION"]),
  ("MaxFloatingObjects", "Objets flottants max","num",  "Debris libres toleres avant nettoyage."),
  ("MaxBackupSaves",     "Sauvegardes de secours","num",""),
 ]),
 ("Performance", [
  ("TrashRemovalEnabled",          "Nettoyage des debris","bool","A LAISSER ACTIF. Sinon les epaves s'accumulent sans fin."),
  ("StopGridsPeriodMin",           "Gel des grilles (min)","num","Fige les grilles inactives depuis N minutes."),
  ("EnableSelectivePhysicsUpdates","Physique selective",  "bool","INCOMPATIBLE avec les mods de rencontres tant que SyncDistance est plafonne a 2000. Laisser desactive."),
  ("ViewDistance",                 "Distance de vue",     "num", ""),
 ]),
]
PLATS = [(k, l, t, a, (c[0] if c else None)) for _, g in REGLAGES for (k, l, t, a, *c) in g]
TYPES = {k: (t, c) for (k, l, t, a, c) in PLATS}

# Reglages qui cassent une partie ou un serveur si on les touche a l'aveugle.
RISQUES = {
    "PermanentDeath": "critique",
    "TrashRemovalEnabled": "critique",
    "EnableSelectivePhysicsUpdates": "critique",
    "EnableRespawnShips": "attention",
    "EnvironmentHostility": "attention",
    "EnableCopyPaste": "attention",
    "EnableIngameScripts": "attention",
    "AutoSaveInMinutes": "attention",
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
    """Compte au mieux les joueurs presents en relisant le journal du serveur.

    Le serveur dedie n'expose pas de compteur : on rejoue les arrivees et les
    departs du dernier journal. Renvoie None si le journal est illisible ou
    muet, l'interface affiche alors une valeur indisponible plutot qu'un
    chiffre invente.
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


def etat(valeurs=None):
    r = subprocess.run(["pgrep", "-f", "SpaceEngineersDedicated.exe"], capture_output=True, text=True)
    en_ligne = bool(r.stdout.strip())
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

  <div class="notice err hide" id="err"><div id="err-txt"></div></div>

  <div class="notice"><div>Appliquer <b>arrete le serveur</b>, ecrit dans les fichiers du monde, puis le relance : les joueurs sont deconnectes et tout ce qui n'a pas ete sauvegarde est perdu. Sans l'option d'arret force, <code>stop.sh</code> refuse de couper quand la derniere sauvegarde depasse <code>SE_SAVE_MAX_AGE</code>, et rien n'est ecrit. Lis l'age de la sauvegarde ci-dessus avant de valider.</div></div>

  <form id="f" autocomplete="off"></form>

  <footer class="meta" id="meta"></footer>
</div>

<div class="console" id="console">
  <div class="bar" aria-hidden="true"></div>
  <div class="console-in">
    <button type="button" class="act" id="appliquer">Appliquer et redemarrer</button>
    <button type="button" class="act ghost" id="recharger">Recharger</button>
    <label class="opt" id="opt-force" title="stop.sh refuse de couper quand la derniere sauvegarde est plus vieille que SE_SAVE_MAX_AGE. Cocher passe outre : tout ce qui a ete construit depuis est perdu.">
      <input type="checkbox" id="forcer"><span>forcer l'arret malgre une sauvegarde ancienne</span>
    </label>
    <span class="diff" id="diff">aucune modification</span>
    <span class="log" id="msg" role="status" aria-live="polite"></span>
  </div>
</div>

<script>
var SCHEMA=[],VALS={},RISQUES={},chrono=null;

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
function bord(r){
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
}

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
  diff();
}
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
el("recharger").onclick=function(){charger();};
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
</script></body></html>"""


def page():
    return PAGE.replace("__MONDE__", html.escape(MONDE))


class H(http.server.BaseHTTPRequestHandler):
    server_version = "SEPanel/1.0"

    def log_message(self, *a):
        pass

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
        if not self.path.startswith("/api/appliquer"):
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

        # Corps attendu : {"valeurs": {...}, "forcer": true|false}. Un objet
        # plat reste accepte et vaut forcer=false : l'arret force ne peut pas
        # etre obtenu par omission.
        vals = recu.get("valeurs")
        if isinstance(vals, dict):
            forcer = bool(recu.get("forcer"))
        else:
            vals, forcer = recu, False

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
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
            if r.returncode != 0:
                detail = " ".join(((r.stdout or "") + " " + (r.stderr or "")).split())
                return self._j({"ok": False, "arretRefuse": True,
                                "erreur": "arret refuse, rien n'a ete ecrit : " + detail[:600]})

            # 2. Ecrire les trois fichiers, puis retirer la copie compressee
            #    que le jeu relirait de preference.
            modifies = ecrire(propres)
            note = supprimer_sbsb5()

            # 3. Relancer.
            subprocess.run(["bash", str(demarrage)], capture_output=True, timeout=1200)
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
    with socketserver.TCPServer((HOTE, PORT), H) as s:
        if not _boucle_locale(HOTE):
            print("ATTENTION : ecoute sur %s, sans authentification." % HOTE, file=sys.stderr)
        print("Panneau de reglages : http://%s:%d" % (HOTE, PORT))
        print("Monde : %s   Racine : %s" % (MONDE, ROOT))
        print("Ctrl+C pour arreter.")
        try:
            s.serve_forever()
        except KeyboardInterrupt:
            print("")
