"""
ReCoder 디스코드 작업 패널 — 버튼/모달/Select 기반 UI.

명령어를 외우는 대신 버튼으로 개발→깃허브→배포→운영을 진행한다.
- [개발]      입력 모달 → 코드 생성(run_generation)
- [배포]      Select(S3 공개 / 로컬 미리보기)
- [전체 실행] 모달 입력 → 개발 → 배포(S3) 파이프라인 + 실시간 진행 임베드
- [깃허브]/[운영]  1학기 범위 안내(2학기 자동화 예정)

배포는 deploy_client(게이트웨이 /deploy/s3)로 처리한다.
"""
from __future__ import annotations

import discord

import deploy_client
import make_handler

try:
    from recoder_bridge import hub
except Exception:  # pragma: no cover
    hub = None
try:
    import guild_store
except Exception:  # pragma: no cover
    guild_store = None

ACCENT = 0x2563EB

# 상태 → 표시 마커 (이모지 아님)
_MARK = {"done": "✓", "running": "●", "pending": "○", "skip": "–", "fail": "✕"}


# ── 임베드 ────────────────────────────────────────────────────────────────────
def build_panel_embed() -> discord.Embed:
    e = discord.Embed(
        title="ReCoder 작업 패널",
        description="버튼으로 진행하세요. **개발 → 깃허브 → 배포 → 운영**.",
        color=ACCENT,
    )
    e.add_field(name="개발", value="자연어로 코드 생성·수정", inline=True)
    e.add_field(name="배포", value="S3 공개 / 로컬 미리보기", inline=True)
    e.add_field(name="전체 실행", value="개발부터 배포까지 한 번에", inline=True)
    return e


def _progress_embed(steps: list[list], url: str = "") -> discord.Embed:
    lines = []
    for name, status, detail in steps:
        mark = _MARK.get(status, "○")
        line = f"`{mark}` **{name}**"
        if detail:
            line += f" — {detail}"
        lines.append(line)
    e = discord.Embed(title="전체 실행 — 진행 상황", description="\n".join(lines), color=ACCENT)
    if url:
        e.add_field(name="공개 주소", value=url, inline=False)
    return e


# ── 배포 공통 ────────────────────────────────────────────────────────────────
def _make_qr_file(url: str):
    """배포 URL을 QR PNG(discord.File)로. 라이브 발표용 — 청중이 스캔해 바로 접속."""
    try:
        import io
        import qrcode
        img = qrcode.make(url)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        return discord.File(buf, filename="deploy-qr.png")
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger("recoder-bot").warning(
            "QR 생성 실패(%s) — 'pip install qrcode Pillow' 후 봇 재시작하세요.", e)
        return None


async def _announce_deploy(interaction: discord.Interaction, url: str):
    """배포 결과를 채널에 공개 게시(URL + QR). 발표 화면에서 보이고 청중이 스캔."""
    qr = _make_qr_file(url)
    text = f"**배포 완료** — 공개 주소\n{url}\nQR을 스캔하면 바로 열립니다."
    try:
        if qr:
            await interaction.channel.send(content=text, file=qr)
        else:
            await interaction.channel.send(content=text)
    except Exception:
        pass


def _current_session(channel_id: int) -> dict | None:
    sess = make_handler._SESSIONS.get(channel_id)
    if sess and sess.get("code"):
        return sess
    return None


async def _deploy_s3(interaction: discord.Interaction) -> str:
    sess = _current_session(interaction.channel.id)
    if not sess:
        raise RuntimeError("아직 생성된 코드가 없습니다. 먼저 [개발]로 만들어 주세요.")
    base = (sess["filename"].rsplit(".", 1)[0]) or "site"
    project = f"{interaction.user.display_name}-{base}"
    files = [{"path": sess["filename"], "content": sess["code"]}]
    data = await deploy_client.deploy_static(project, files)
    return data["url"]


async def _preview_local(interaction: discord.Interaction) -> bool:
    """저장된 코드를 학생 VSCode로 다시 보내 브라우저로 열게 한다(auto_run)."""
    sess = _current_session(interaction.channel.id)
    if not sess or hub is None:
        raise RuntimeError("아직 생성된 코드가 없습니다. 먼저 [개발]로 만들어 주세요.")
    target = ""
    if guild_store is not None:
        try:
            target = guild_store.get_student_id(interaction.user.id) or ""
        except Exception:
            target = ""

    async def emit(ev):
        if target:
            return await hub.send_to_student(target, ev)
        return await hub.broadcast(ev)

    if target and not hub.student_connected(target):
        raise RuntimeError(f"연결된 VSCode(student `{target}`)를 찾을 수 없습니다.")
    if not target and hub.connected_count == 0:
        raise RuntimeError("VSCode 확장이 브리지에 연결되어 있지 않습니다.")

    fn, lang, code = sess["filename"], sess["language"], sess["code"]
    await emit({"type": "start", "filename": fn, "language": lang, "prompt": ""})
    await emit({"type": "chunk", "text": code})
    await emit({"type": "end", "filename": fn, "auto_run": True})
    return True


# ── 배포 Select ──────────────────────────────────────────────────────────────
class DeploySelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="S3에 공개 배포", value="s3",
                                 description="인터넷에서 열리는 공개 URL을 만듭니다"),
            discord.SelectOption(label="로컬 미리보기", value="local",
                                 description="내 VSCode에서 바로 엽니다"),
        ]
        super().__init__(placeholder="배포 방식을 선택하세요…", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        choice = self.values[0]
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            if choice == "s3":
                url = await _deploy_s3(interaction)
                await _announce_deploy(interaction, url)
                await interaction.followup.send("배포 완료 — 채널에 주소·QR을 게시했어요.", ephemeral=True)
            else:
                await _preview_local(interaction)
                await interaction.followup.send("VSCode에서 미리보기를 열었습니다.", ephemeral=True)
        except Exception as e:  # noqa: BLE001
            await interaction.followup.send(f"실패: {e}", ephemeral=True)


class DeployView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.add_item(DeploySelect())


# ── 개발/ALL 입력 모달 ──────────────────────────────────────────────────────
class DevModal(discord.ui.Modal):
    prompt = discord.ui.TextInput(
        label="무엇을 만들까요?",
        style=discord.TextStyle.paragraph,
        placeholder="예: 테트리스 게임 / 할 일 목록 앱",
        max_length=500,
        required=True,
    )

    def __init__(self, mode: str = "dev"):
        super().__init__(title="ReCoder — 개발" if mode == "dev" else "ReCoder — 전체 실행")
        self.mode = mode

    async def on_submit(self, interaction: discord.Interaction):
        text = str(self.prompt.value).strip()
        await interaction.response.defer(thinking=True)
        if self.mode == "all":
            await _run_all(interaction, text)
            return
        # 단순 개발
        res = await make_handler.run_generation(interaction.channel, text, interaction.user.id)
        if not res.get("ok"):
            await interaction.followup.send(f"생성 실패: {res.get('error','알 수 없는 오류')}")
            return
        await interaction.followup.send(
            f"`{res['filename']}` 생성 완료. 배포하시겠어요?",
            view=DeployView(),
        )


async def _run_all(interaction: discord.Interaction, prompt: str):
    steps = [["개발", "running", ""], ["깃허브", "pending", ""],
             ["배포", "pending", ""], ["운영", "pending", ""]]
    msg = await interaction.followup.send(embed=_progress_embed(steps), wait=True)

    res = await make_handler.run_generation(interaction.channel, prompt, interaction.user.id)
    if not res.get("ok"):
        steps[0] = ["개발", "fail", (res.get("error", "") or "")[:80]]
        await msg.edit(embed=_progress_embed(steps))
        return
    steps[0] = ["개발", "done", res["filename"]]
    steps[1] = ["깃허브", "skip", "VSCode에서 진행 (2학기 자동화)"]
    steps[2] = ["배포", "running", "S3 업로드 중…"]
    await msg.edit(embed=_progress_embed(steps))

    try:
        url = await _deploy_s3(interaction)
        steps[2] = ["배포", "done", "공개 완료"]
    except Exception as e:  # noqa: BLE001
        steps[2] = ["배포", "fail", str(e)[:80]]
        await msg.edit(embed=_progress_embed(steps))
        return
    steps[3] = ["운영", "skip", "2학기"]
    await msg.edit(embed=_progress_embed(steps, url=url))
    await _announce_deploy(interaction, url)


# ── 패널 View ────────────────────────────────────────────────────────────────
class PanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=600)

    @discord.ui.button(label="개발", style=discord.ButtonStyle.primary)
    async def dev(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(DevModal(mode="dev"))

    @discord.ui.button(label="깃허브", style=discord.ButtonStyle.secondary)
    async def github(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "깃허브 연동은 VSCode 워크벤치 → GitHub 탭에서 진행하세요. (2학기 디스코드 자동화 예정)",
            ephemeral=True,
        )

    @discord.ui.button(label="배포", style=discord.ButtonStyle.secondary)
    async def deploy(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "배포 방식을 선택하세요:", view=DeployView(), ephemeral=True,
        )

    @discord.ui.button(label="운영", style=discord.ButtonStyle.secondary)
    async def operate(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "운영(모니터링·장애 대응)은 서버 배포가 필요해 2학기 범위입니다.",
            ephemeral=True,
        )

    @discord.ui.button(label="전체 실행 (ALL)", style=discord.ButtonStyle.success, row=1)
    async def run_all(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(DevModal(mode="all"))


async def send_panel(interaction: discord.Interaction):
    """/recoder panel 명령에서 호출 — 작업 패널을 띄운다."""
    await interaction.response.send_message(embed=build_panel_embed(), view=PanelView(), ephemeral=True)
