import random
import discord
import math
from datetime import timedelta

from discord.ext import commands
from dotenv import load_dotenv
from discord import app_commands
import os
import webserver

load_dotenv()

TOKEN = os.environ['discordkey']

import json

LEADERBOARD_FILE = "leaderboard.json"

import asyncio

guess_locks = {}

ACTIVE_GAMES_FILE = "active_ranked_games.json"


def save_active_games():
    with open(ACTIVE_GAMES_FILE, "w") as f:
        json.dump(
            {
                str(uid): {
                    "channel_id": game["channel_id"],
                    "guesses": game["guesses"],
                    "finished": game["finished"]
                }
                for uid, game in ranked_games.items()
                if not game.get("finished")
            },
            f,
            indent=2
        )


def load_active_games():
    if not os.path.exists(ACTIVE_GAMES_FILE):
        return {}

    try:
        with open(ACTIVE_GAMES_FILE) as f:
            content = f.read().strip()
            if not content:
                os.remove(ACTIVE_GAMES_FILE)
                return {}

            return json.loads(content)

    except json.JSONDecodeError:
        os.remove(ACTIVE_GAMES_FILE)
        return {}


def load_leaderboard():
    if not os.path.exists(LEADERBOARD_FILE):
        return {}
    with open(LEADERBOARD_FILE, "r") as f:
        return json.load(f)


def save_leaderboard():
    with open(LEADERBOARD_FILE, "w") as f:
        json.dump(leaderboard_data, f, indent=2)


leaderboard_data = load_leaderboard()


def get_user_stats(user_id):
    user_id = str(user_id)

    if user_id not in leaderboard_data:
        leaderboard_data[user_id] = {
            "elo": 1000,
            "wins": 0,
            "losses": 0
        }
        save_leaderboard()

    return leaderboard_data[user_id]


RANKS = [
    (0, "Bigma", "<:caseoh:1347767315084873859>"),
    (500, "Bronze", "🥉"),
    (1200, "Silver", "🥈"),
    (1500, "Gold", "🥇"),
    (1800, "Platinum", "🔷"),
    (2100, "Diamond", "💎"),
    (2400, "Mythic", "🐉"),
    (2700, "Master", "🔥"),
    (3000, "Grandmaster", "👑"),
    (3300, "Legend", "⚡"),
    (3600, "Godlike", "⚜️"),
    (4000, "Immortal", "🌌"),
    (4500, "Celestial", "🔱"),
    (5100, "Ascendant", "🌠"),
    (5800, "Transcendent", "🔮"),
    (6500, "Absolute", "⚫"),
    (7200, "Overlord", "🧙‍♂️"),
    (7900, "Empyrean", "☀️"),
    (8600, "Cosmic", "🪐"),
    (9300, "Astral", "✨"),
    (10000, "Singularity", "🌀"),
    (676767, "ohio sigma rizzler", "🗿"),
    (696969, "elaine", "🥰"),
]

DIVISIONS = 5


def get_rank(elo):
    current = RANKS[0]
    for r in RANKS:
        if elo >= r[0]:
            current = r
        else:
            break
    return current  # (threshold, name, emoji)


def get_next_rank(elo):
    for r in RANKS:
        if elo < r[0]:
            return r
    return None


def get_major_rank(elo):
    current = RANKS[0]
    for r in RANKS:
        if elo >= r[0]:
            current = r
        else:
            break
    return current


def major_rank_start(elo):
    return get_rank(elo)[0]


def get_next_major_rank(elo):
    for r in RANKS:
        if elo < r[0]:
            return r
    return None


def get_rank_with_division(elo):
    for i in range(len(RANKS) - 1, -1, -1):
        start, name, emoji = RANKS[i]
        end = RANKS[i + 1][0] if i + 1 < len(RANKS) else 0

        if elo >= start:
            span = end - start
            div_size = span / DIVISIONS

            # raw division
            if end > elo:
                div = int((elo - start) / div_size) + 1
                div = max(1, min(DIVISIONS, div))
            else:
                div = 0

            return (start, end, name, emoji, div)

    return None


def rank_key(rank_tuple):
    """
    rank_tuple from get_rank_with_division: (start, end, name, emoji, div)
    higher is better.
    """
    start, end, name, emoji, div = rank_tuple
    # index of major rank in RANKS list
    major_index = next(i for i, r in enumerate(RANKS) if r[0] == start)
    return (major_index, div)


def did_rank_up(old_rank, new_rank):
    if not old_rank or not new_rank:
        return False
    return rank_key(new_rank) > rank_key(old_rank)


def did_rank_down(old_rank, new_rank):
    if not old_rank or not new_rank:
        return False
    return rank_key(new_rank) < rank_key(old_rank)


def progress_bar(current, start, end, length=30):
    if end == start:
        filled = length
    else:
        progress = (current - start) / (end - start)
        progress = max(0, min(1, progress))
        filled = int(progress * length)

    return "█" * filled + "░" * (length - filled)


# ----------------------------------------------------------- ELO SYSTEM -----------------------------------------------------

# ----- ELO TUNING -----

ELO_CENTER = 2500  # where ladder stabilizes
ELO_WIN_FLOOR = 2000  # wins never lose elo below ts

K_MAX = 22  # volatility at very low elo
K_MIN = 2  # 3                  # volatility at very high elo
K_DECAY_POWER = 3.4  # 1.5        # how fast K shrinks

NEUTRAL_BASE = 7  # neutral guesses at elo=0
NEUTRAL_DROP = 4.2  # how much neutral shrinks at high elo

LOSS_GUESSES = 7  # treat loss as this many guesses
LOSS_SCALE_POWER = 2.1  # how much harsher losses get at high elo

LOW_ELO_WIN_BONUS = 30  # max bonus for bad-but-winning games

MAX_GAIN = 250
MAX_LOSS = -75


def neutral_guesses(elo):
    return NEUTRAL_BASE - NEUTRAL_DROP * (elo / (elo + ELO_CENTER))


def k_factor(elo):
    return K_MIN + (K_MAX - K_MIN) / (1 + (elo / ELO_CENTER) ** K_DECAY_POWER)


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def performance_bonus(elo, guesses):
    if guesses >= 4:
        return 1.0

    scale = max(0.0, (ELO_CENTER - elo) / ELO_CENTER)

    if guesses == 3:
        return 1.0 + 0.1 * scale
    if guesses == 2:
        return 1.0 + 0.95 * scale


def ranked_elo_delta(elo, guesses, won):
    g = guesses if won else LOSS_GUESSES

    base = k_factor(elo) * (neutral_guesses(elo) - g)
    print(base)

    # scale losses by elo
    if not won:
        print("imgi")
        loss_scale = (elo / ELO_WIN_FLOOR) ** LOSS_SCALE_POWER
        print(loss_scale)
        base *= loss_scale
        print(base)

    # wins never lose elo below threshold
    if won and elo < ELO_WIN_FLOOR:
        base = max(base, 0)

    # 4 guesses always safe
    if won and guesses == 4:
        base = max(base, 0)

    # bonus for cracked games
    if won:
        base *= performance_bonus(elo, guesses)

    # low-elo pity gain for ugly wins
    if won and elo < ELO_WIN_FLOOR and base == 0:
        bonus_scale = 1 - (elo / ELO_WIN_FLOOR)
        base += LOW_ELO_WIN_BONUS * bonus_scale

    base = clamp(base, MAX_LOSS, MAX_GAIN)

    return round(base)


# def new_ranked_elo_delta(elo, guesses, won):
#     g = guesses if won else 7
#     t = (elo - (500 * ((7 - g) ** 1.6))) / 5600
#     h = -800 * math.sin(t)
#     return max(-75, round(h)) if g > 4 else max(1, round(h))

def new_ranked_elo_delta(elo, guesses, won):
    g = guesses if won else 7
    t = (elo - (500 * ((7 - g) ** 1.6))) / 5600
    h = -800 * math.sin(t)
    print(h)
    # softener: reduces how brutal bad outcomes are
    GRIND_SOFTENER = 0.25
    bounded_change_by_g = {1: 150, 2: 50, 3: 15, 4: 3, 5: 35, 6: 55, 7: 75}
    if g > 4:
        # different caps for 5/6/7 so they are different this comment is really mart

        h = h * GRIND_SOFTENER
        print(h)
        cap = -bounded_change_by_g[g]
        return max(cap, round(h))

    # good wins
    if g < 5:
        return min(max(bounded_change_by_g[g], round(h)), MAX_GAIN)
    return min(max(15, round(h)), MAX_GAIN)


def cooked_ranked_elo_delta(elo, guesses, won):
    # --- constants ---
    LOSS_SCALE = 0.35  # scales losses only

    # --- original function (unchanged) ---
    def old_func(elo, g):
        t = (elo - (500 * ((7 - g) ** 1.6))) / 5600
        h = -800 * math.sin(t)
        return h

    # --- new damped function (for g > 4) ---
    def new_func(elo, g):
        A = 800
        B = 2
        C = 0.09
        D = 3
        P = 1
        p = 2
        c = 0.8
        phi = 1.9

        t = (elo - 500 * ((7 - g) ** 1.6)) / 5600
        tp = max(0.0, t)

        term1 = 1 - (tp ** p) / (tp ** p + c ** p)
        term2 = (C ** D) / (tp ** D + C ** D)
        term3 = (math.sin(B * (t ** P) - phi) + 1) / 2
        offset = 15 / (g - 1)

        return A * term1 * term2 * term3 + offset

    # --- game logic ---
    g = guesses if won else 7

    if won and g <= 4:
        h = old_func(elo, g)
        return max(1, round(h))

    h = new_func(elo, g)

    # scale losses only
    if not won:
        h *= LOSS_SCALE
        return max(-150, round(h))

    return max(1, round(h))


# ---------- load word lists ----------
with open("answers.txt") as f:
    ANSWERS = [line.strip() for line in f]

with open("words.txt") as f:
    WORDS = set(line.strip() for line in f)


# ---------- wordle logic ----------
yellowEmojis = {"a": "<:yellow_a:1471405850584420372>", "b": "<:yellow_b:1471405852027125832>", "c": "<:yellow_c:1471405852945551361>", "d": "<:yellow_d:1471405854103179348>", "e": "<:yellow_e:1471405854652633110>", "f": "<:yellow_f:1471405855885758629>", "g": "<:yellow_g:1471405856842322021>", "h": "<:yellow_h:1471405858952052848>", "i": "<:yellow_i:1471405859962884211>", "j": "<:yellow_j:1471405861342810258>", "k": "<:yellow_k:1471405862546571305>", "l": "<:yellow_l:1471405869332959436>", "m": "<:yellow_m:1471405870767407105>", "n": "<:yellow_n:1471405871799079103>", "o": "<:yellow_o:1471405873065623632>", "p": "<:yellow_p:1471405880812769426>", "q": "<:yellow_q:1471405882641219678>", "r": "<:yellow_r:1471405883601850439>", "s": "<:yellow_s:1471405885548007578>", "t": "<:yellow_t:1471405886655299721>", "u": "<:yellow_u:1471405887963795497>", "v": "<:yellow_v:1471405889712816290>", "w": "<:yellow_w:1471405890744881246>", "x": "<:yellow_x:1471405894674944145>", "y": "<:yellow_y:1471405896138752145>", "z": "<:yellow_z:1471405898231709696>"}
greenEmojis = {"a": "<:green_a:1471405772356456643>", "b": "<:green_b:1471405773488914554>", "c": "<:green_c:1471405774554271849>", "d": "<:green_d:1471405776106033176>", "e": "<:green_e:1471405777481895937>", "f": "<:green_f:1471405778953830606>", "g": "<:green_g:1471405781071953992>", "h": "<:green_h:1471405782141632686>", "i": "<:green_i:1471405783437541557>", "j": "<:green_j:1471405784670670972>", "k": "<:green_k:1471405786386268202>", "l": "<:green_l:1471405787724382218>", "m": "<:green_m:1471405789100118048>", "n": "<:green_n:1471405790542823538>", "o": "<:green_o:1471405791826415666>", "p": "<:green_p:1471405792971325563>", "q": "<:green_q:1471405794598584517>", "r": "<:green_r:1471405795621998634>", "s": "<:green_s:1471405796880547860>", "t": "<:green_t:1471405798272925739>", "u": "<:green_u:1471405800206635150>", "v": "<:green_v:1471405801401876520>", "w": "<:green_w:1471405803150774282>", "x": "<:green_x:1471405804015059092>", "y": "<:green_y:1471405805554241557>", "z": "<:green_z:1471405806829436949>"}
grayEmojis = {"a": "<:gray_a:1471405723865972780>", "b": "<:gray_b:1471405725912793260>", "c": "<:gray_c:1471405726781149205>", "d": "<:gray_d:1471405728018464800>", "e": "<:gray_e:1471405728974770216>", "f": "<:gray_f:1471405730115354654>", "g": "<:gray_g:1471405731805925387>", "h": "<:gray_h:1471405720393220207>", "i": "<:gray_i:1471405733240377499>", "j": "<:gray_j:1471405734343348359>", "k": "<:gray_k:1471405735903494398>", "l": "<:gray_l:1471405737036222517>", "m": "<:gray_m:1471405738181267518>", "n": "<:gray_n:1471405721462509660>", "o": "<:gray_o:1471405739380707408>", "p": "<:gray_p:1471405740919885835>", "q": "<:gray_q:1471405742467584010>", "r": "<:gray_r:1471405743763881994>", "s": "<:gray_s:1471405722720796786>", "t": "<:gray_t:1471405744317399092>", "u": "<:gray_u:1471405747416862802>", "v": "<:gray_v:1471405748729937920>", "w": "<:gray_w:1471405750243819645>", "x": "<:gray_x:1471405751422418965>", "y": "<:gray_y:1471405752794091583>", "z": "<:gray_z:1471405760251564045>"}

def green(word, answer):
    return [word[i] == answer[i] for i in range(5)]


def yellow(word, answer, greenlist):
    yellowlist = [False] * 5
    remaining = []

    for i in range(5):
        if not greenlist[i]:
            remaining.append(answer[i])

    for i in range(5):
        if not greenlist[i] and word[i] in remaining:
            yellowlist[i] = True
            remaining.remove(word[i])

    return yellowlist
KEYBOARD_ROWS = [
    "qwertyuiop",
    "asdfghjkl",
    "zxcvbnm"
]


def format_result(word, greenlist, yellowlist):
    emojis = []
    for i in range(5):
        if greenlist[i]:
            emojis.append(greenEmojis[word[i]])
        elif yellowlist[i]:
            emojis.append(yellowEmojis[word[i]])
        else:
            emojis.append(grayEmojis[word[i]])
    return "".join(emojis)



PRIORITY = {
    "⬜": 0,
    "⬛": 1,
    "🟨": 2,
    "🟩": 3
}

ROW_INDENTS = [
    0,  # qwertyuiop
    5,  # asdfghjkl
    15  # zxcvbnm
]


def empty_keyboard():
    return {chr(c): "⬜" for c in range(ord("a"), ord("z") + 1)}


def update_keyboard(kb, word, greenlist, yellowlist):
    for i, letter in enumerate(word):
        if greenlist[i]:
            new = "🟩"
        elif yellowlist[i]:
            new = "🟨"
        else:
            new = "⬛"

        if PRIORITY[new] > PRIORITY[kb[letter]]:
            kb[letter] = new


def render_keyboard(kb):
    lines = []
    for row, indent in zip(KEYBOARD_ROWS, ROW_INDENTS):
        keys = []
        for letter in row:
            state = kb[letter]
            keys.append(f"{state}**{letter.upper()}**")
        line = " ".join(keys)
        lines.append(" " * indent + line)
    return "\n".join(lines)


# ---------- discord bot ----------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

games = {}
ranked_games = {}


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    await tree.sync()

    interrupted = load_active_games()
    if not interrupted:
        return

    for uid_str, data in interrupted.items():
        user_id = int(uid_str)

        stats = get_user_stats(user_id)

        COMP_ELO = new_ranked_elo_delta(stats["elo"], interrupted[uid_str]["guesses"] + 1, True)
        COMP_ELO = max(0, min(30, COMP_ELO))
        stats["elo"] += COMP_ELO
        save_leaderboard()

        channel = bot.get_channel(data["channel_id"])
        if channel:
            await channel.send(
                f"**bot restarted**\n"
                f"<@{user_id}> uhh i kinda cleared ur ranked game bc i updated the bot whops \n"
                f"bc im a kind individual i compensate u **+{COMP_ELO} elo** :3"
            )

    os.remove(ACTIVE_GAMES_FILE)

@bot.hybrid_command(name = "wordle", description = "start a wordle game")
async def wordle(ctx: commands.Context):

    if ctx.author.id in ranked_games:
        await ctx.send("you already in a ranked game you little dodger", ephemeral=True)
        return

    games[ctx.author.id] = {
        "answer": random.choice(ANSWERS),
        "guesses": 0,
        "keyboard": empty_keyboard(),
        "guessString": ""
    }
    if ctx.channel.id == 1293326543346466969:
        await ctx.send("**casual wordl started!!**\nuse `/guess <word>`", ephemeral=True)
    else:
        await ctx.send("**casual wordl started!!**\nuse `/guess <word>` or `!guess <word>`", ephemeral=True)

@bot.command()
async def isgay(ctx):
    gayness = random.randint(0, 101)
    await ctx.send(f"u r {gayness}% gay. ur gay bro just admit it. :3")

@bot.hybrid_command(name = "wordleranked", description = "start a ranked wordle game")
async def wordleranked(ctx: commands.Context):
    if ctx.author.id in ranked_games:
        await ctx.send("you already in a ranked game you little dodger", ephemeral=True)
        return

    get_user_stats(ctx.author.id)

    answer = random.choice(ANSWERS)

    ranked_games[ctx.author.id] = {
        "answer": answer,
        "guesses": 0,
        "keyboard": empty_keyboard(),
        "guessString": "",
        "channel_id": ctx.channel.id,
        "finished": False
    }

    save_active_games()

    print(f"[RANKED] {ctx.author} -> answer: {answer}")
    if ctx.channel.id == 1293326543346466969:
        await ctx.send("**RANKED wordl started!!!!** \nuse `/guess <word>`")
    else:
        await ctx.send("**RANKED wordl started!!!!** \nuse `/guess <word>` or `!guess <word>`")



@bot.hybrid_command(name = "guess", description = "guess a word in your current wordle game")
async def guess(ctx: commands.Context, word: str):
    if ctx.interaction or ctx.channel.id != 1293326543346466969:
        pass
    else:
        await ctx.send(f"`!guess` does not work in {ctx.channel.mention}, use `/guess` instead")
        return

    user_id = ctx.author.id

    if user_id not in guess_locks:
        guess_locks[user_id] = asyncio.Lock()

    async with guess_locks[user_id]:

        try:
            if user_id in ranked_games:
                game = ranked_games[user_id]
                ranked = True
            elif user_id in games:
                game = games[user_id]
                ranked = False
            else:
                await ctx.send("start a game first u mart (with `!wordle` or `!wordleranked`)", ephemeral=True)
                return

            answer = game["answer"]

            word = word.lower()
            if len(word) != 5 or word not in WORDS:
                await ctx.send("not a valid 5-letter word you bigma", ephemeral=True)
                return

            game["guesses"] += 1
            if ranked:
                ranked_games[user_id]["guesses"] = game["guesses"]
                save_active_games()

            if word == answer:
                if ranked:
                    stats = get_user_stats(user_id)
                    old_elo = stats["elo"]

                    if game.get("finished"):
                        return

                    game["finished"] = True

                    delta = new_ranked_elo_delta(old_elo, game["guesses"], won=True)

                    stats["elo"] += delta
                    stats["wins"] += 1
                    save_leaderboard()

                    old = get_rank_with_division(old_elo)
                    new_actual = get_rank_with_division(stats["elo"])

                    rankup_msg = ""

                    if old and new_actual:
                        if did_rank_up(old, new_actual):
                            rankup_msg = (
                                f"\n\n🎉 **RANK UP!** 🎉\n"
                                f"{old[3]} **{old[2]} {old[4]}** -> "
                                f"{new_actual[3]} **{new_actual[2]} {new_actual[4]}**"
                            )
                        elif did_rank_down(old, new_actual):
                            rankup_msg = (
                                f"\n\n💀 **RANK DOWN** 💀\n"
                                f"{old[3]} **{old[2]} {old[4]}** -> "
                                f"{new_actual[3]} **{new_actual[2]} {new_actual[4]}**"
                            )

                    # ---- DISPLAY ONLY ----
                    new = new_actual
                    if old and new and old[0] != new[0]:
                        new = (new[0], new[1], new[2], new[3], 1)

                    await ctx.send(
                        f"**RANKED WIN** 🟩\n"
                        f"word: `{answer.upper()}`\n"
                        f"guesses: {game['guesses']}\n"
                        f"elo: `{delta:+}` -> **{stats['elo']}**"
                        f"{rankup_msg}"
                    )
                    save_active_games()
                    del ranked_games[user_id]
                else:
                    await ctx.send(f"**casual win** 🟩 word was `{answer.upper()}`", ephemeral=True)
                    del games[user_id]
                return

            greenlist = green(word, answer)
            yellowlist = yellow(word, answer, greenlist)

            result = format_result(word, greenlist, yellowlist)
            if game["guesses"] > 1:
                game["guessString"] = f"{game['guessString']}\n{result}"
            else: game["guessString"] = result

            update_keyboard(game["keyboard"], word, greenlist, yellowlist)
            keyboard_display = render_keyboard(game["keyboard"])

            await ctx.send(
                f"**__guess #{game['guesses']}__**\n"
                f"{game['guessString']}\n\n"
                f"**__keyboard__**\n{keyboard_display}", ephemeral=True
            )

            if game["guesses"] >= 6:
                if ranked:
                    stats = get_user_stats(user_id)
                    old_elo = stats["elo"]

                    if game.get("finished"):
                        return

                    game["finished"] = True

                    delta = new_ranked_elo_delta(stats["elo"], 6, won=False)

                    stats["elo"] += delta
                    stats["losses"] += 1
                    save_leaderboard()

                    old = get_rank_with_division(old_elo)
                    new = get_rank_with_division(stats["elo"])

                    rankdown_msg = ""
                    if old and new:
                        if did_rank_down(old, new):
                            rankdown_msg = (
                                f"\n\n💀 **RANK DOWN** 💀\n"
                                f"{old[3]} **{old[2]} {old[4]}** -> {new[3]} **{new[2]} {new[4]}**"
                            )

                    await ctx.send(
                        f"**RANKED LOSS** 💀\n"
                        f"word was `{answer.upper()}`\n"
                        f"elo: `{delta}` -> **{stats['elo']}**"
                        f"{rankdown_msg}"
                    )
                    save_active_games()
                    del ranked_games[user_id]
                else:
                    await ctx.send(f"**casual loss** 💀 word was `{answer.upper()}`", ephemeral=True)
                    del games[user_id]
        finally:
            # cleanup only if this lock is no longer needed
            if not guess_locks[user_id].locked():
                guess_locks.pop(user_id, None)

@bot.command()
async def elo(ctx, member: discord.Member = None):
    member = member or ctx.author
    stats = get_user_stats(member.id)

    rank = get_rank(stats["elo"])

    await ctx.send(
        f"**{member.display_name}**\n"
        f"rank: {rank[2]} **{rank[1]}**\n"
        f"elo: **{stats['elo']}**\n"
        f"wins: {stats['wins']} | losses: {stats['losses']}"
    )


@bot.command()
async def leaderboard(ctx):
    if not leaderboard_data:
        await ctx.send("noobdy in leaderboard")
        return

    ranked = sorted(
        leaderboard_data.items(),
        key=lambda x: x[1]["elo"],
        reverse=True
    )

    total_players = len(ranked)
    top_10 = ranked[:10]

    user_rank = None
    for i, (uid, _) in enumerate(ranked, start=1):
        if int(uid) == ctx.author.id:
            user_rank = i
            break

    leaderboard_string = ""
    for i, (user_id, stats) in enumerate(top_10, start=1):
        member = ctx.guild.get_member(int(user_id))
        name = f"<@{user_id}>"

        rank_info = get_rank_with_division(stats["elo"])
        _, _, rank_name, emoji, div = rank_info

        ROMAN = ["I", "II", "III", "IV", "V"]
        div_str = ROMAN[div - 1] if div != 0 else ""

        leaderboard_string += f"**#{i}** {emoji} **{name}**\nElo: `{stats['elo']}` • *{rank_name} {div_str}*\n\n"

    embed = discord.Embed(
        title="__wordl leaderboard__",
        description=leaderboard_string if leaderboard_string else "No data available.",
        color=discord.Color.gold()
    )

    footer_text = (
        f"You are ranked #{user_rank} out of {total_players} players"
        if user_rank else
        "You are not ranked yet"
    )
    embed.set_footer(text=footer_text)
    try:
        embed.set_thumbnail(url = ctx.guild.icon.url)
    except:
        print("Server does not have a custom PFP.")
    await ctx.send(embed=embed)


@bot.command()
async def result(ctx, elo: int, outcome: str, guesses: int = None):
    outcome = outcome.lower()

    if outcome not in ("win", "lose"):
        await ctx.send("usage: `!result <elo> win <guesses>` or `!result <elo> lose`")
        return

    won = outcome == "win"

    if won and guesses is None:
        await ctx.send("for wins, include guesses: `!result 1000 win 4`")
        return

    if not won:
        guesses = LOSS_GUESSES

    # BEFORE
    old_elo = elo
    old_rank = get_rank_with_division(old_elo)

    # SIMULATE GAME
    delta = new_ranked_elo_delta(old_elo, guesses, won)
    new_elo = old_elo + delta
    new_rank_actual = get_rank_with_division(new_elo)

    # RANK CHANGE CHECK
    rankup_msg = ""

    if old_rank and new_rank_actual:
        if did_rank_up(old_rank, new_rank_actual):
            rankup_msg = (
                f"\n\n🎉 **RANK UP!** 🎉\n"
                f"{old_rank[3]} **{old_rank[2]} {old_rank[4]}** "
                f"→ {new_rank_actual[3]} **{new_rank_actual[2]} {new_rank_actual[4]}**"
            )
        elif did_rank_down(old_rank, new_rank_actual):
            rankup_msg = (
                f"\n\n💀 **RANK DOWN** 💀\n"
                f"{old_rank[3]} **{old_rank[2]} {old_rank[4]}** "
                f"→ {new_rank_actual[3]} **{new_rank_actual[2]} {new_rank_actual[4]}**"
            )

    await ctx.send(
        f"**elo sim**\n"
        f"start elo: `{old_elo}`\n"
        f"result: `{outcome}`\n"
        f"guesses: `{guesses}`\n"
        f"Δ elo: `{delta:+}` → `{new_elo}`"
        f"{rankup_msg}"
    )


@bot.command()
async def rank(ctx, member: discord.Member = None):
    member = member or ctx.author
    stats = get_user_stats(member.id)

    elo = stats["elo"]
    start, end, name, emoji, div = get_rank_with_division(elo)
    if div == 0:
        div = ""
    next_major = get_next_major_rank(elo)

    if next_major:
        bar = progress_bar(elo, start, next_major[0])
        progress = (
            f"{emoji} **{name} {div}** "
            f"`{bar}` "
            f"{next_major[2]} **{next_major[1]}**\n"
            f"`{start}`{' ' * 84}`{next_major[0]}`"
            #  (len(bar)*3 - 6)
        )
    else:
        progress = f"{emoji} **{name} {div}** `{'█' * 30}` 👑 **MAX**"

    await ctx.send(
        f"**{member.display_name}**\n"
        f"rank: {emoji} **{name} {div}**\n"
        f"elo: **{elo}**\n\n"
        f"**progress to next rank**\n"
        f"{progress}"
    )


@bot.command()
async def ranks(ctx):
    lines = []

    for i in range(len(RANKS)):
        start, name, emoji = RANKS[i]
        end = RANKS[i + 1][0] - 1 if i + 1 < len(RANKS) else "∞"

        lines.append(
            f"{emoji} **{name}** - `{start}` -> `{end}`"
        )

    await ctx.send(
        "**__wordle ranks__**\n"
        + "\n".join(lines)
    )

@tree.command(name = "application", description = "creates server application")
async def application(interaction: discord.Interaction, username: str, name: str):
    if interaction.user.id == 881239314246287360 and interaction.guild.id == 1264709544349536277:
        duration = timedelta(hours = 168)
        question = f"Should {username} ({name}) be allowed to join the server?"
        poll = discord.Poll(question = question, duration = duration)
        firstName = name.split(maxsplit = 1)[0] if name else "Albert"
        poll.add_answer(text = f"Yes, {firstName} should be allowed to join the server.")
        poll.add_answer(text = f"No, {firstName} should not be allowed to join the server.")
        server_application_thread = interaction.guild.get_thread(1360501829477072897)
        await server_application_thread.send(poll = poll)
        await server_application_thread.send("@everyone", allowed_mentions = discord.AllowedMentions(everyone = True))
        await interaction.response.send_message("ok i made poll you mart")
    else:
        await interaction.response.send_message("using this command is a crime (its fine nobodys gonna know)", ephemeral = True)

# ---------- run ----------
webserver.keep_alive()
bot.run(TOKEN)