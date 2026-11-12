# 隔离 Claude Code Docker 运行块

本文件定义交给用户手动执行的 Claude Code 隔离运行方式。这里的 Docker 命令用于启动 Claude Code 工作容器，不是项目自身的业务 Compose 启动命令。

## 固定要求

- 每道新题都创建新的容器和新运行目录，不能复用已启动过的容器。
- bind mount 的工作区在启动时必须完全为空，包括隐藏文件；不得在 `docker run` 前复制源码。
- 已有初始代码时，必须在容器里的 Claude 进程就绪后自动导入 `/workspace`，导入完成后用户再发送题目。
- 生成型任务没有初始代码时，启动空工作区后直接发送题目。
- 多个任务同时运行时，每个任务使用独立终端、独立运行目录和唯一容器名。
- 容器命名固定为 `claude-<项目编号>-<YYYYMMDDHHMMSS>`，运行目录固定为 `run-<项目编号>-<随机后缀>`；项目编号直接使用题号，例如 `gb-537`。
- 保留 `--init`、`--restart=no`、`--cap-drop ALL`、`--security-opt no-new-privileges`，不挂载 Docker Socket，也不授予额外 capability。
- 不使用 `--rm`；会话结束后先从容器导出原生轨迹，再删除容器。
- 不使用 `docker start`、`docker restart`、`--continue` 或 `--resume`。
- 每台设备必须有一个唯一用户名和独立的 Keychain service。API Key 从 `~/.codex/benzhi-claude-device.json` 对应的 Keychain 条目读取，绝不把真实 Key 写进技能、状态文件、回复或命令行历史。
- 用户名只用于隔离本机凭据，上游身份仍由 API Key 决定。切换设备时必须使用该设备自己的 Key，不能复制另一台设备的 Key。
- 启动命令、轨迹导出命令和清理命令由用户手动执行，Codex 不代替用户启动会话。

## 一次性保存 Key

技能自带 macOS Keychain 配置脚本，用户只需在当前设备执行一次。终端会安全提示输入该设备自己的 Key，不会把 Key 写进命令历史或技能文件：

```bash
bash scripts/configure-macos-keychain.sh
```

脚本会自动生成唯一用户名和 Keychain service，并在 `~/.codex/benzhi-claude-device.json` 记录设备身份。当前设备可以显式固定用户名：

```bash
bash scripts/configure-macos-keychain.sh --username gaobo-pi-a453493f
```

也可以只登记身份、不保存 Key：

```bash
bash scripts/configure-macos-keychain.sh --register-only
```

配置完成后，Keychain 条目使用设备用户名作为 account，例如：

```bash
security add-generic-password -a "$DEVICE_ACCOUNT" -s "$KEYCHAIN_SERVICE" -U -w
```

## 已有初始代码的启动命令

启动时工作区保持为空。后台任务等待容器里的 Claude 进程就绪，再导入初始代码；因此不会触发工作区必须为空的入口检查。看到导入成功提示后再发送题目。

```bash
PROJECT_ID="gb-537"
PROJECT_SRC="/Users/gaobo/repositories/gitlab/评审项目/project/solo-0910/sources/gb-537"
CONTAINER_NAME="claude-${PROJECT_ID}-$(date +%Y%m%d%H%M%S)"
BASE_DIR="$HOME/claude-runs"

mkdir -p "$BASE_DIR" || exit 1
RUN_DIR="$(mktemp -d "$BASE_DIR/run-${PROJECT_ID}-XXXXXXXX")" || exit 1
mkdir "$RUN_DIR/workspace" || exit 1

IDENTITY_SCRIPT="${CODEX_HOME:-$HOME/.codex}/skills/solo-annotation-loop/scripts/device-identity.py"
eval "$(python3 "$IDENTITY_SCRIPT" --format shell)"
API_KEY="$(security find-generic-password -a "$DEVICE_ACCOUNT" -s "$KEYCHAIN_SERVICE" -w 2>/dev/null || true)"
if [[ -z "$API_KEY" ]]; then
  echo "未找到当前设备 Key，请先执行 $IDENTITY_SCRIPT 对应的 configure-macos-keychain.sh --username $DEVICE_USERNAME"
  exit 1
fi

printf '容器：%s\n本题本地目录：%s\n' "$CONTAINER_NAME" "$RUN_DIR"

(
  set -o pipefail
  for _ in $(seq 1 120); do
    if docker exec "$CONTAINER_NAME" pgrep -x claude >/dev/null 2>&1; then
      if ! docker exec "$CONTAINER_NAME" sh -lc 'test -z "$(find /workspace -mindepth 1 -print -quit)"'; then
        echo '初始代码导入失败：工作区已非空。' >&2
        exit 1
      fi
      tar \
        --exclude='.env' \
        --exclude='node_modules' \
        --exclude='dist' \
        --exclude='.vite' \
        --exclude='*.tsbuildinfo' \
        --exclude='._*' \
        --exclude='.DS_Store' \
        -C "$PROJECT_SRC" -cf - . | docker exec -i "$CONTAINER_NAME" tar -C /workspace -xf -
      echo '初始代码已导入 /workspace，可以开始输入题目。' >&2
      exit 0
    fi
    sleep 1
  done
  echo '初始代码导入失败：等待 Claude 就绪超时。' >&2
  exit 1
) &
IMPORTER_PID=$!

docker run -it --init --restart=no --cap-drop ALL --security-opt no-new-privileges \
  --name "$CONTAINER_NAME" \
  --mount "type=bind,src=$RUN_DIR/workspace,dst=/workspace" \
  -e "apikey=$API_KEY" \
  adminfather/benzhi-claude-code:20260909-isolated-git

wait "$IMPORTER_PID" 2>/dev/null || true
```

## 纯生成任务的启动命令

工作区没有初始代码时，删除上面的后台导入块，只保留空目录、Keychain 和 `docker run`。容器进入 Claude 界面后直接发送题目。

## 多任务并行

每个任务打开独立终端，每个终端设置自己的 `PROJECT_ID` 和 `PROJECT_SRC`。容器名、运行目录和后台导入进程都各自独立。第二个终端只改项目变量，不要把第一个终端的 `RUN_DIR` 复制过去。

```bash
PROJECT_ID="gb-540"
PROJECT_SRC="/Users/gaobo/repositories/gitlab/评审项目/project/solo-0910/sources/gb-540"
CONTAINER_NAME="claude-${PROJECT_ID}-$(date +%Y%m%d%H%M%S)"
BASE_DIR="$HOME/claude-runs"

mkdir -p "$BASE_DIR" || exit 1
RUN_DIR="$(mktemp -d "$BASE_DIR/run-${PROJECT_ID}-XXXXXXXX")" || exit 1
mkdir "$RUN_DIR/workspace" || exit 1

IDENTITY_SCRIPT="${CODEX_HOME:-$HOME/.codex}/skills/solo-annotation-loop/scripts/device-identity.py"
eval "$(python3 "$IDENTITY_SCRIPT" --format shell)"
API_KEY="$(security find-generic-password -a "$DEVICE_ACCOUNT" -s "$KEYCHAIN_SERVICE" -w 2>/dev/null || true)"
if [[ -z "$API_KEY" ]]; then
  echo "未找到当前设备 Key，请先执行 $IDENTITY_SCRIPT 对应的 configure-macos-keychain.sh --username $DEVICE_USERNAME"
  exit 1
fi

printf '容器：%s\n本题本地目录：%s\n' "$CONTAINER_NAME" "$RUN_DIR"

(
  set -o pipefail
  for _ in $(seq 1 120); do
    if docker exec "$CONTAINER_NAME" pgrep -x claude >/dev/null 2>&1; then
      if ! docker exec "$CONTAINER_NAME" sh -lc 'test -z "$(find /workspace -mindepth 1 -print -quit)"'; then
        echo '初始代码导入失败：工作区已非空。' >&2
        exit 1
      fi
      tar \
        --exclude='.env' \
        --exclude='node_modules' \
        --exclude='dist' \
        --exclude='.vite' \
        --exclude='*.tsbuildinfo' \
        --exclude='._*' \
        --exclude='.DS_Store' \
        -C "$PROJECT_SRC" -cf - . | docker exec -i "$CONTAINER_NAME" tar -C /workspace -xf -
      echo '初始代码已导入 /workspace，可以开始输入题目。' >&2
      exit 0
    fi
    sleep 1
  done
  echo '初始代码导入失败：等待 Claude 就绪超时。' >&2
  exit 1
) &
IMPORTER_PID=$!

docker run -it --init --restart=no --cap-drop ALL --security-opt no-new-privileges \
  --name "$CONTAINER_NAME" \
  --mount "type=bind,src=$RUN_DIR/workspace,dst=/workspace" \
  -e "apikey=$API_KEY" \
  adminfather/benzhi-claude-code:20260909-isolated-git

wait "$IMPORTER_PID" 2>/dev/null || true
```

## 多轮边界

同一个任务继续下一轮时，留在当前容器和当前终端继续对话，不要退出后 `docker start`，也不要新建容器。新任务必须回到本节命令，创建新的空运行目录和新的容器名。容器一旦退出，当前镜像不支持继续或恢复。

断联或中断后重新触发时，输入内容只能是“继续”两个字，不得附加任何引导、提示、安抚、补充要求或对上一轮任务的复述，避免给被测模型额外线索。这条续跑输入不算轮次：不递增轮次编号，不新增交付表行，本轮提示词和 TurnID/PromptID 保持不变，只在轨迹里记一次续跑。

## 会话结束后

在启动该容器的原终端执行，变量必须仍保留：

```bash
docker cp "$CONTAINER_NAME:/home/node/.claude/projects" "$RUN_DIR/traces" || {
  echo '轨迹导出失败，不要删除容器，修复后重新导出。'
  exit 1
}

if ! find "$RUN_DIR/traces" -type f -name '*.jsonl' -print -quit | grep -q .; then
  echo '未找到轨迹 JSONL，不要删除容器。'
  exit 1
fi

find "$RUN_DIR/traces" -type f -name '*.jsonl' -print
docker rm "$CONTAINER_NAME"
```

代码在本机 `$RUN_DIR/workspace`，轨迹在本机 `$RUN_DIR/traces`。确认两者完整后再删除容器；不要把不同任务的轨迹导出到同一个目录。
