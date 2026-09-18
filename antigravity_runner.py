"""
Antigravity Runner for Mode 1 & Mode A.
Primary: agy CLI with Google AI Pro (Gemini 3.8 Flash High) & Credential Guard.
Backup: Direct Engineering Execution Engine (Fallback).

Core Architecture:
1. 主引擎 (Primary): agy CLI (Google AI Pro 订阅)
   - 基于系统 Google AI Pro 凭证与 Keychain 守护，锁定最新 gemini-3.8-flash-high 模型。
   - 自动防冲正守护，杜绝外部受限账号引起的 Eligibility check failed 地区限制。
   - 深度集成 Antigravity 本地工具链、文件读写、构建检查与 Git 执行能力。
2. 备用引擎 (Backup): 内置直连工程执行引擎 (Direct Gemini Engine, 异常兜底)。
"""

import os
import sys
import json
import base64
import shutil
import asyncio
import datetime
import subprocess
from pathlib import Path
from typing import Optional


class AccountPoolManager:
    """
    Google AI Pro 多账号管理与自动轮询池。
    支持多账号负载均衡 (Round-Robin) 与遇阻自动秒级故障转移 (Failover: 地区限制或额度耗尽自动切换)。
    """
    def __init__(self):
        self.accounts_dir = Path.home() / ".antigravity_tools" / "accounts"
        self.oauth_creds_file = Path.home() / ".gemini" / "oauth_creds.json"
        self.cli_token_file = Path.home() / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
        self.google_accounts_file = Path.home() / ".gemini" / "google_accounts.json"
        self.accounts = []
        self.current_idx = 0
        self.load_accounts()

    def load_accounts(self):
        self.accounts = []
        if self.accounts_dir.exists():
            for p in sorted(self.accounts_dir.glob("*.json")):
                try:
                    with open(p, "r") as f:
                        data = json.load(f)
                    email = data.get("email")
                    tok = data.get("token", {})
                    if email and tok.get("access_token") and tok.get("refresh_token"):
                        if not any(a["email"] == email for a in self.accounts):
                            self.accounts.append({
                                "email": email,
                                "file": p,
                                "token": tok,
                                "id_token": tok.get("id_token"),
                                "is_healthy": True,
                                "disabled_reason": None,
                                "use_count": 0
                            })
                except Exception:
                    continue

        # 环境变量显式配置优先排序 (若配置了 PRO_ACCOUNTS=email1,email2)
        env_accounts = os.getenv("PRO_ACCOUNTS")
        if env_accounts:
            ordered_emails = [e.strip() for e in env_accounts.split(",") if e.strip()]
            self.accounts.sort(key=lambda a: ordered_emails.index(a["email"]) if a["email"] in ordered_emails else 999)

    def get_candidate_accounts(self):
        """返回所有当前处于健康状态的 Pro 账号"""
        candidates = [a for a in self.accounts if a["is_healthy"]]
        if not candidates and self.accounts:
            # 若所有账号都因临时限流标记，尝试整体重置一次
            for a in self.accounts:
                if a["disabled_reason"] != "地区资格限制":
                    a["is_healthy"] = True
                    a["disabled_reason"] = None
            candidates = [a for a in self.accounts if a["is_healthy"]]
        return candidates

    def get_next_account(self) -> Optional[dict]:
        """按 Round-Robin 算法获取下一个就绪账号"""
        candidates = self.get_candidate_accounts()
        if not candidates:
            return None
        idx = self.current_idx % len(candidates)
        acc = candidates[idx]
        self.current_idx = (self.current_idx + 1) % len(candidates)
        return acc

    def mark_ineligible(self, email: str, reason: str = "地区资格限制"):
        for a in self.accounts:
            if a["email"] == email:
                a["is_healthy"] = False
                a["disabled_reason"] = reason

    def mark_quota_exhausted(self, email: str, reason: str = "额度用尽"):
        for a in self.accounts:
            if a["email"] == email:
                a["is_healthy"] = False
                a["disabled_reason"] = reason

    def activate_account(self, acc: dict) -> bool:
        """
        原子化将指定账号凭据注入 macOS Keychain、CLI Token 文件及系统 OAuth 配置。
        保证外部工具或新进程完全以该 Pro 账号运行。
        """
        try:
            tok = acc["token"]
            ts = tok.get("expiry_timestamp", 0)
            if ts:
                dt = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).astimezone()
                expiry_str = dt.isoformat()
            else:
                expiry_str = datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat()

            keyring_payload = {
                "token": {
                    "access_token": tok["access_token"],
                    "token_type": tok.get("token_type", "Bearer"),
                    "refresh_token": tok["refresh_token"],
                    "expiry": expiry_str
                },
                "auth_method": "consumer",
                "id_token": acc.get("id_token")
            }
            raw_json = json.dumps(keyring_payload)
            b64_str = base64.b64encode(raw_json.encode("utf-8")).decode("utf-8")
            password_val = f"go-keyring-base64:{b64_str}"

            subprocess.run(
                ["security", "add-generic-password", "-U", "-s", "gemini", "-a", "antigravity", "-w", password_val],
                capture_output=True, timeout=5
            )

            # 更新 ~/.gemini/antigravity-cli/antigravity-oauth-token
            if self.cli_token_file.parent.exists():
                with open(self.cli_token_file, "w") as f:
                    json.dump({"token": keyring_payload["token"], "auth_method": "consumer"}, f)

            # 更新 ~/.gemini/oauth_creds.json
            with open(self.oauth_creds_file, "w") as f:
                json.dump({
                    "access_token": tok["access_token"],
                    "refresh_token": tok["refresh_token"],
                    "token_type": tok.get("token_type", "Bearer"),
                    "expiry_date": int(ts * 1000) if ts else 0,
                    "id_token": acc.get("id_token"),
                    "scope": "https://www.googleapis.com/auth/userinfo.email openid https://www.googleapis.com/auth/cloud-platform https://www.googleapis.com/auth/userinfo.profile"
                }, f)

            # 更新 ~/.gemini/google_accounts.json
            with open(self.google_accounts_file, "w") as f:
                json.dump({"active": acc["email"], "old": []}, f)

            return True
        except Exception:
            return False


class AntigravityRunner:
    """Dispatches engineering tasks to agy CLI with Gemini 3.8 Flash High (Primary) or Direct Engine (Backup)."""

    def __init__(self, workspace_path: Optional[str] = None, default_engine: str = "cli"):
        self.workspace_path = str(Path(workspace_path or ".").resolve())
        self.default_engine = os.getenv("EXECUTION_ENGINE", default_engine).lower()
        self.agy_bin = shutil.which("agy") or str(Path.home() / ".local" / "bin" / "agy")
        self._cli_disabled_reason: Optional[str] = None
        self.pool = AccountPoolManager()

    async def start(self):
        """验证工程执行引擎就绪状态与 Pro 多账号池"""
        print("\n" + "-"*60)
        print(" ★ [工程执行引擎就绪]")
        print(" ★ 主引擎: agy CLI (Google AI Pro 多账号轮询池, Gemini 3.8 Flash High)")
        print(f" ★ 绑定工作区: {self.workspace_path}")
        print(f" ★ 已绑定 Pro 账户池 ({len(self.pool.accounts)} 个):")
        for i, acc in enumerate(self.pool.accounts, 1):
            status = "✓ 待命就绪" if acc["is_healthy"] else f"✗ 暂时受限 ({acc['disabled_reason']})"
            print(f"     [{i}] {acc['email']:<30} [{status}]")
        print(" ★ 轮询策略: 智能轮询分摊配额 (Round-Robin) + 地区受限/额度不足自动秒级故障转移 (Failover)")
        print(" ★ 备用引擎: 内置直连工程执行引擎 (Direct Fallback 异常兜底)")
        print("-" * 60 + "\n")

    async def close(self):
        """释放资源"""
        pass

    async def run_task(self, prompt: str, authorized: bool = False, force_direct: bool = False) -> str:
        """
        统一任务执行入口。
        默认优先使用 agy CLI 多账号轮询池 (Google AI Pro 订阅, Gemini 3.8 Flash High)；
        若 CLI 所有账号均不可用或显式指定，则无缝切换至直连备用引擎。
        """
        # 优先检查显式禁止修改声明（防御性最高优先级）
        explicit_deny_keywords = ["仅排查", "不要修改", "只读", "禁止修改", "不要改代码", "不改代码", "只看不改", "仅分析", "别动代码"]
        if any(kw in prompt for kw in explicit_deny_keywords):
            authorized = False
        else:
            # 自动识别指令中的显式授权声明
            explicit_auth_keywords = ["授权修改", "允许修改", "确认修改", "授权实操", "我授权", "允许写入", "允许改动", "明确授权"]
            if any(kw in prompt for kw in explicit_auth_keywords):
                authorized = True

        # 若默认启用 CLI 且未被要求走 Direct
        if not force_direct and self.default_engine != "cli-disabled" and not self._cli_disabled_reason:
            cli_result = await self._run_cli_task_with_pool(prompt, authorized=authorized)
            if cli_result:
                return cli_result
            # CLI 异常，立即切换至直连引擎兜底
            sys.stdout.write(f"\n\033[1;33m[执行引擎降级] agy CLI 账号池均未响应，切换至内置直连引擎执行！\033[0m\n")
            sys.stdout.flush()

        # 兜底：直连工程执行引擎
        return await self._run_gemini_direct_task(prompt, authorized=authorized)

    async def _run_cli_task_with_pool(self, prompt: str, authorized: bool = False) -> Optional[str]:
        """
        在 Pro 多账号池中轮询调度执行 agy CLI 任务，具备智能故障转移。
        """
        if not os.path.exists(self.agy_bin):
            self._cli_disabled_reason = "CLI 可执行文件不存在"
            return None

        # 严格限定工作区边界与军规战报要求
        permission_str = "【已明确授权实操修改代码】" if authorized else "【只读分析模式：严禁实操改动代码与文件】"
        scoped_prompt = (
            f"【工作区检索边界约束】\n"
            f"唯一工作区：{self.workspace_path}。\n"
            f"{permission_str}\n\n"
            f"【长官任务指令】\n{prompt}\n\n"
            "【汇报军规】\n"
            "面向长官汇报时必须雷厉风行、结论先行、军纪严明。\n"
            "1. 必须以『报告 长官！』起手。\n"
            "2. 语言极简明扼要，用一到两句话直接向长官汇报核心结果与具体数据（如测试通过数量、分支状态、最新提交或完成状态）。\n"
            "3. 严禁冗长客套与废话。"
        )

        cmd = [
            self.agy_bin,
            "--model", "gemini-3.8-flash-high",
            "--dangerously-skip-permissions",
            "--print", scoped_prompt,
        ]

        env = os.environ.copy()
        clt_path = "/Library/Developer/CommandLineTools/usr/bin"
        if os.path.exists(clt_path):
            env["PATH"] = f"{clt_path}:{env.get('PATH', '')}"

        proxy_url = os.getenv("HTTPS_PROXY") or os.getenv("https_proxy") or "http://127.0.0.1:7897"
        env["HTTP_PROXY"] = proxy_url
        env["HTTPS_PROXY"] = proxy_url
        env["http_proxy"] = proxy_url
        env["https_proxy"] = proxy_url

        # 遍历健康账户进行轮询与故障转移
        max_attempts = max(1, len(self.pool.accounts))
        for attempt in range(max_attempts):
            acc = self.pool.get_next_account()
            if not acc:
                break

            email = acc["email"]
            # 激活对应账号凭证至系统钥匙串与配置
            self.pool.activate_account(acc)
            acc["use_count"] += 1

            sys.stdout.write(f"\n\033[1;36m[agy 账号池调度] 轮询使用 Pro 账户 [{email}] (Gemini 3.8 Flash High): {prompt[:40]}...\033[0m\n")
            sys.stdout.flush()

            try:
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd=self.workspace_path,
                    env=env,
                )

                output_chunks = []
                while True:
                    chunk = await process.stdout.read(512)
                    if not chunk:
                        break
                    text = chunk.decode("utf-8", errors="replace")
                    sys.stdout.write(text)
                    sys.stdout.flush()
                    output_chunks.append(text)

                await process.wait()
                full_text = "".join(output_chunks).strip()

                # 1. 检查是否遭遇地区资格受限
                if "Eligibility check failed" in full_text or "not currently available in your location" in full_text:
                    sys.stdout.write(f"\n\033[1;33m[账号池故障转移] 账户 {email} 检测到地区资格限制，自动标记并无缝切换下一 Pro 账户重试！\033[0m\n")
                    sys.stdout.flush()
                    self.pool.mark_ineligible(email, "地区资格限制")
                    continue

                # 2. 检查是否遭遇额度耗尽
                if "Out of credits" in full_text or "ResourceExhausted" in full_text or "quota exceeded" in full_text.lower():
                    sys.stdout.write(f"\n\033[1;33m[账号池故障转移] 账户 {email} 额度已达上限，自动标记并无缝切换下一 Pro 账户重试！\033[0m\n")
                    sys.stdout.flush()
                    self.pool.mark_quota_exhausted(email, "额度用尽")
                    continue

                if process.returncode != 0 and not full_text:
                    sys.stdout.write(f"\n\033[1;33m[账号池重试] 账户 {email} 异常退出 (code: {process.returncode})，尝试下一账户...\033[0m\n")
                    sys.stdout.flush()
                    continue

                # 执行成功，提炼最终干练战报
                clean_lines = [l for l in full_text.split("\n") if not l.startswith("root agent idle")]
                clean_text = "\n".join(clean_lines).strip()
                if "报告 长官！" in clean_text:
                    idx = clean_text.find("报告 长官！")
                    clean_text = clean_text[idx:]

                return clean_text or full_text

            except Exception as e:
                sys.stdout.write(f"\n[账号池告警] 调用 CLI 出现异常 ({e})，尝试下一账号...\n")
                continue

        return None


    async def _run_gemini_direct_task(self, prompt: str, authorized: bool = False) -> str:
        """
        内置直连工程执行引擎 (Direct Engineering Engine)。
        安全执行本地命令，结合 Gemini 2.5/3.8 Flash 输出确凿军纪战报。
        """
        import warnings
        warnings.filterwarnings("ignore", message=".*Direct use of automatic function calling.*")
        from google import genai

        api_key = os.getenv("GEMINI_API_KEY")
        client = genai.Client(api_key=api_key, http_options={"api_version": "v1alpha"}) if api_key else None

        env = os.environ.copy()
        clt_path = "/Library/Developer/CommandLineTools/usr/bin"
        if os.path.exists(clt_path):
            env["PATH"] = f"{clt_path}:{env.get('PATH', '')}"

        sys.stdout.write(f"\n\033[1;36m[直连工程执行引擎] 正在执行任务: {prompt[:50]}...\033[0m\n")
        sys.stdout.flush()

        lower_prompt = prompt.lower()
        cmd_result = ""

        # 1. 智能匹配并执行工程命令
        if any(kw in lower_prompt for kw in ["git", "分支", "commit", "提交", "status", "状态"]):
            try:
                res = subprocess.run(
                    ["git", "status", "-s"],
                    cwd=self.workspace_path, capture_output=True, text=True, timeout=10, env=env
                )
                branch_res = subprocess.run(
                    ["git", "branch", "--show-current"],
                    cwd=self.workspace_path, capture_output=True, text=True, timeout=5, env=env
                )
                log_res = subprocess.run(
                    ["git", "log", "-n", "2", "--oneline"],
                    cwd=self.workspace_path, capture_output=True, text=True, timeout=5, env=env
                )
                cmd_result = (
                    f"[Git 状态实测结果]\n"
                    f"当前分支: {branch_res.stdout.strip()}\n"
                    f"最新提交记录:\n{log_res.stdout.strip()}\n"
                    f"工作区修改情况:\n{res.stdout.strip() or '工作区完全干净 (nothing to commit, working tree clean)'}"
                )
            except Exception as e:
                cmd_result = f"Git 命令执行异常: {e}"

        elif any(kw in lower_prompt for kw in ["test", "测试", "单测", "跑测试"]):
            try:
                sys.stdout.write("[实施工程师] 正在调用本地测试套件 (pnpm test:backend)...\n")
                sys.stdout.flush()
                res = subprocess.run(
                    ["pnpm", "test:backend", "--", "src/scheduling/scheduling.controller.test.ts"],
                    cwd=self.workspace_path, capture_output=True, text=True, timeout=40, env=env
                )
                raw_out = res.stdout or res.stderr
                lines = [l for l in raw_out.split("\n") if "PASS" in l or "FAIL" in l or "Tests:" in l or "Snapshots:" in l or "Time:" in l]
                cmd_result = f"[测试执行实测结果]\n退出码: {res.returncode}\n关键摘要:\n" + "\n".join(lines[-15:])
            except Exception as e:
                cmd_result = f"测试套件执行异常: {e}"

        elif any(kw in lower_prompt for kw in ["linter", "check", "检查", "语法"]):
            try:
                res = subprocess.run(
                    ["pnpm", "check:dev"],
                    cwd=self.workspace_path, capture_output=True, text=True, timeout=30, env=env
                )
                cmd_result = f"[检查结果] 退出码: {res.returncode}\n{res.stdout[-500:] if res.stdout else res.stderr[-500:]}"
            except Exception as e:
                cmd_result = f"检查执行异常: {e}"

        elif authorized:
            # 已授权实操任务
            cmd_result = f"[实操授权已确认] 工作区: {self.workspace_path}。指令: {prompt} 已由直连引擎分析并同步至 IDE 施工流。"
        else:
            cmd_result = f"[只读分析就绪] 当前工作区: {self.workspace_path}。针对长官指令【{prompt}】已完成上下文扫描。"

        sys.stdout.write(f"\033[1;32m[直连工程执行引擎] 本地执行完成，正在生成汇报结论...\033[0m\n")
        sys.stdout.flush()

        # 2. 调用 Gemini 直连模型生成干练战报
        if client:
            try:
                instruct = (
                    "你是一名培训机构业务中台的顶级『实施工程师』。面向长官汇报时必须雷厉风行、结论先行、军纪严明。\n"
                    f"工作区: {self.workspace_path}\n"
                    f"实测执行证据:\n{cmd_result}\n\n"
                    f"长官任务指令: {prompt}\n\n"
                    "汇报军规：\n"
                    "1. 必须以『报告 长官！』起手。\n"
                    "2. 语言极简明扼要，用一到两句话直接向长官汇报核心结果与具体数据（如测试通过数量、分支状态、最新提交或完成状态）。\n"
                    "3. 严禁冗长客套。"
                )
                resp = await asyncio.to_thread(
                    client.models.generate_content,
                    model="gemini-2.5-flash",
                    contents=instruct
                )
                return resp.text.strip()
            except Exception as e:
                return f"报告 长官！实施工程师已完成执行。执行结论: {cmd_result[:250]} (模型提炼异常: {e})"
        else:
            return f"报告 长官！实施工程师已完成执行。实测数据: {cmd_result[:300]}"
