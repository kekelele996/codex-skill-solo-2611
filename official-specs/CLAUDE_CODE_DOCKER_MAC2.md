# Claude Code 使用说明（Mac）

**一份镜像，每道题新建一个容器，容器内始终使用 `/workspace`。不需要题号。**

流程：新建本地空工作目录并启动容器 → 做题，代码直接保存在本机 → 退出并导出轨迹 → 删除本题容器。下一题重复相同流程，使用新的空工作目录和会话记录。

## 1. 准备

安装并启动 Docker Desktop，准备管理员发放的 API Key。按 `Command + 空格`，搜索并打开“终端”。下面的 Docker 命令都在 Mac 终端执行。

镜像：`adminfather/benzhi-claude-code2:20261110`。Intel 和 Apple 芯片使用同一镜像名、同一组命令，Docker 自动选择匹配的架构。

镜像内 Claude Code 版本：`2.1.319`。

网关 `https://llm2.jzxhnh.com`、模型 `auto_model/urm` 和 Git 环境已配置在镜像内。无需在 Mac 安装 Claude Code 或 Git，也不需要下载启动脚本。

## 2. 映射本地目录，启动容器

把 `xxxxx` 换成完整 Key，保留英文引号：

```bash
mkdir -p "$HOME/claude-runs" && \
RUN_DIR="$(mktemp -d "$HOME/claude-runs/run-XXXXXXXX")" && \
mkdir "$RUN_DIR/workspace" && \
printf '本题本地目录：%s\n' "$RUN_DIR" && \
docker run -it --init --restart=no --cap-drop ALL --security-opt no-new-privileges --name claude-task --mount "type=bind,src=$RUN_DIR/workspace,dst=/workspace" -e "apikey=xxxxx" adminfather/benzhi-claude-code2:20261110
```

首次运行会下载镜像，随后直接进入 Claude 对话，无需其他启动命令。看到输入框后输入本题内容。

若出现权限确认，请阅读后再决定是否继续。此环境允许 Claude 自动执行命令、修改文件和访问网络，仅用于本题，不要放入无关敏感资料。

- 本机的 `$RUN_DIR/workspace` 映射到容器内的 `/workspace`，启动时必须为空，包括隐藏文件。
- 例如本机目录是 `~/claude-runs/run-AbCd1234/workspace`，容器内仍然只叫 `/workspace`，不需要题号。
- 轨迹保存在 `/home/node/.claude/projects`。
- 只映射本次新建的工作目录，不挂载本机用户目录、Claude/Codex 配置或上道题目录。
- `claude-task` 只是本次容器的固定名称，每道题都可以使用这个名称，不是题号。

`--mount` 中的 `src` 是本机目录，`dst=/workspace` 是容器内目录。代码会直接出现在本机对应文件夹，不需要再复制出来。若要更换存放位置，把命令中的两处 `$HOME/claude-runs` 改成自己选择的绝对路径即可，不要使用现有项目目录。

**这是双向读写映射，不是备份：容器内修改或删除文件，会直接影响本机映射目录；在本机修改同一目录，容器中也会看到变化。**

## 3. 结束对话，导出轨迹

等待 Claude 完成操作，在对话输入框中按两次 `Ctrl+D`，直到回到 Mac 终端。此模式禁用了斜杠命令，不使用 `/exit`。

退出后容器停止，代码已经保存在本机，轨迹仍留在容器中。不要在启动命令中添加 `--rm`，否则退出时会删除尚未导出的轨迹。

在同一个 Mac 终端执行：

```bash
docker cp claude-task:/home/node/.claude/projects "$RUN_DIR/traces"
```

打开启动时显示的本题本地目录：`workspace` 是已直接保存的代码，`traces` 是导出的完整轨迹。`run-...` 后缀只用于避免本机文件相互覆盖，不是题号，容器内始终使用 `/workspace`。

请保留同一个终端窗口，以便沿用第 2 节设置的 `RUN_DIR`。核对代码和轨迹，移除敏感信息后再提交或压缩；轨迹请保留完整目录结构。导出报错时，先解决并重新导出，不要删除容器。

## 4. 删除本题容器，开始下一题

**确认本机代码和导出的轨迹完整后**，执行：

```bash
docker rm claude-task
```

删除容器不会删除本机的本题目录。下一题重新执行第 2 节的整组命令，会自动新建空的本机 `workspace` 并创建新容器，用户目录和会话记录也重新初始化。不要只重复最后一行并沿用上一题的 `RUN_DIR`。

不要用 `docker start` 或 `docker restart` 开始下一题：它们保留容器文件，不会清空环境；本镜像也会拒绝在同一个容器中再次启动会话。不支持 `--continue` 或 `--resume`。

## 同时运行多个容器与切换映射地址

前面的固定名称 `claude-task` 适合一次运行一个容器。需要多开时，**每个容器打开一个独立终端窗口**，使用下面的模板代替第 2 节的启动命令。

### 哪些字段需要替换

| 字段或命令 | 多开或切换路径时怎么改 |
|---|---|
| `CONTAINER_NAME` / `--name` | 每个同时存在的容器必须使用不同名称，例如 `claude-a`、`claude-b`；已停止但未删除的容器也占用名称。 |
| `BASE_DIR` | 本机保存结果的根目录，改成想使用的绝对路径。多个容器可以使用同一个根目录。 |
| `RUN_DIR` / `--mount` 的 `src` | 每个容器必须使用不同的新建空工作目录。下方模板会自动生成，无需手动编号。 |
| `docker cp` 冒号前的容器名 | 必须和本次 `--name` 一致，不能一直写 `claude-task`。下方导出命令使用变量自动对应。 |
| `docker cp` 最后的本机路径 | 使用本次的 `$RUN_DIR/traces`，不要把不同容器的轨迹导出到同一个目录。 |
| `docker rm`、`docker exec` 后的容器名 | 同样替换为要操作的那个容器名称，不要误操作其他容器。 |

**不需要修改：** `dst=/workspace`、容器内轨迹路径 `/home/node/.claude/projects`、镜像名称。容器名只是区分同时运行的环境，不是题号；每个容器内仍统一使用 `/workspace`。

### 多开启动模板

第一个终端使用 `claude-a`。把 `xxxxx` 换成完整 Key；要换保存位置，只改 `BASE_DIR` 一行：

```bash
CONTAINER_NAME="claude-a"
BASE_DIR="$HOME/claude-runs"

mkdir -p "$BASE_DIR" && \
RUN_DIR="$(mktemp -d "$BASE_DIR/run-XXXXXXXX")" && \
mkdir "$RUN_DIR/workspace" && \
printf '容器：%s\n本题本地目录：%s\n' "$CONTAINER_NAME" "$RUN_DIR" && \
docker run -it --init --restart=no --cap-drop ALL --security-opt no-new-privileges --name "$CONTAINER_NAME" --mount "type=bind,src=$RUN_DIR/workspace,dst=/workspace" -e "apikey=xxxxx" adminfather/benzhi-claude-code2:20261110
```

第二个终端执行相同模板，把第一行改为 `CONTAINER_NAME="claude-b"`；其他容器依此使用尚未占用的名称。`BASE_DIR` 可以相同，因为每次都会生成不同的 `run-.../workspace`。不要把第一个终端已经生成的 `RUN_DIR` 复制给第二个容器。

### 映射地址如何切换

例如希望保存到桌面，把模板中的根目录改成：

```bash
BASE_DIR="$HOME/Desktop/claude-runs"
```

然后用修改后的值执行整组启动命令。新目录会是 `$HOME/Desktop/claude-runs/run-.../workspace`，并通过 `src=$RUN_DIR/workspace` 映射到容器的 `/workspace`。路径包含空格时也要保留双引号；不要选择包含逗号的路径，以免与 `--mount` 的字段分隔符混淆。

**修改变量不会改变已经创建的容器挂载。** 需要给同一个容器名称换地址时，先结束该容器的对话，导出轨迹并确认结果完整，再删除该容器，使用新地址重新创建。不能靠重启切换路径，也不要直接挂载上一题的非空目录。原本的本机文件会留在原地址，不会自动搬到新地址。

### 多开时分别导出轨迹

每个容器的对话结束后，在它原来的终端窗口执行下面的命令，**代替第 3 节中写死 `claude-task` 的导出命令**：

```bash
docker cp "${CONTAINER_NAME}:/home/node/.claude/projects" "$RUN_DIR/traces"
```

每个终端保留各自的 `CONTAINER_NAME` 和 `RUN_DIR`，因此代码和轨迹会一一对应。例如：

| 容器 | 本机代码目录 | 本机轨迹目录 |
|---|---|---|
| `claude-a` | `~/claude-runs/run-Ab12Cd34/workspace` | `~/claude-runs/run-Ab12Cd34/traces` |
| `claude-b` | `~/claude-runs/run-Ef56Gh78/workspace` | `~/claude-runs/run-Ef56Gh78/traces` |

表中的目录后缀只是示例，以各终端启动时打印的实际路径为准。确认本次代码和轨迹完整后，使用下面的命令代替第 4 节的固定名称删除命令：

```bash
docker rm "$CONTAINER_NAME"
```

不要关闭原终端或在同一个终端中覆盖变量后再导出。若另开辅助终端执行 `docker exec`，新终端不会自动继承这些变量，请直接写实际容器名，例如 `docker exec -it claude-a bash`。

## Git 使用

镜像包含 Git 工具集、Git LFS、Git SVN、Git 邮件工具、SSH 客户端和 GPG。可以直接要求 Claude 在 `/workspace` 中执行 Git 操作，例如克隆仓库、查看差异、创建分支、提交、合并、变基及推送。

需要自己操作时，在对话仍运行期间另开一个 Mac 终端，执行：

```bash
docker exec -it claude-task bash
```

进入后已经位于 `/workspace`，可以执行 `git --version`、`git lfs version` 等命令。输入 `exit` 只退出这个辅助 Shell，不结束 Claude 对话。

提交需要在本题仓库中设置自己的 `user.name` 和 `user.email`。私有仓库拉取、远端推送和签名需要相应网络、权限及凭据；镜像不内置任何人的 Git 账号或私钥。GitHub CLI（`gh`）不是 Git 命令，不包含在本镜像中。图形界面工具不属于本终端使用流程。

## 隔离范围与排错

入口禁用自动记忆、技能、自定义指令和 MCP 加载，不挂载宿主配置，不继承上一题会话。但题目文本、克隆的代码或调用方主动传入的内容仍然可见，不能保证输出中绝不会出现某个词或技能名称。请勿把其他任务的对话或配置拼入题目。

- **Docker 连接失败：** 启动 Docker Desktop，等待引擎就绪。
- **容器名称已占用：** 检查是否仍在做题或尚未导出。先完成导出，再删除本次 `claude-task`；不要强制删除未备份容器，也不要删除其他项目容器。
- **Key 或网络错误：** 核对 Key 和网络；分享日志前脱敏。结束后仍需先导出需要保留的文件，再删除本题容器并重新创建。
- **轨迹为空：** 确认至少完成了一次对话。仅打开程序不一定产生轨迹。
- **映射目录找不到或拒绝访问：** 确认第 2 节的目录创建命令执行成功，且 Docker Desktop 允许访问所选本机路径。不要用放开整个用户目录权限的方式处理。
- **提示工作目录不为空：** 包括隐藏文件在内，映射目录必须为空。下一题执行第 2 节的整组命令创建新目录，不要为了启动而删除上一题未备份的文件。
- **本机目录会自动清空吗：** 不会。删除或重建容器都不会清空本机目录，因此每次使用新的空目录；之前的结果保留在原目录。

Key 会保存在终端历史和本题容器配置中。不要分享含真实 Key 的命令、截图、配置或轨迹。
