#!/usr/bin/env node
import fs from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { pathToFileURL } from "node:url";

async function loadArtifactTool() {
  try {
    return await import("@oai/artifact-tool");
  } catch (error) {
    const modules = process.env.CODEX_NODE_MODULES || "/Users/gaobo/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules";
    const fallback = path.join(modules, "@oai", "artifact-tool", "dist", "artifact_tool.mjs");
    try {
      return await import(pathToFileURL(fallback).href);
    } catch {
      throw new Error(`无法加载 @oai/artifact-tool，请设置 CODEX_NODE_MODULES。原始错误: ${error.message}`);
    }
  }
}

const { FileBlob, SpreadsheetFile } = await loadArtifactTool();

const DEFAULT_HEADERS = [
  "User Prompt", "SessionID", "TurnID/PromptID", "轮次", "初始环境快照", "提交地址", "轨迹文件",
  "环境可复现等级", "Harness", "Harness 版本", "操作系统", "任务类型", "任务难度",
  "语言/框架", "交付完整性", "交付完整性 - 描述", "指令遵循", "指令遵循 - 描述",
  "任务规划", "任务规划 - 描述", "推理能力", "推理能力 - 描述", "执行能力",
  "执行能力 - 描述", "其他问题", "提交人", "提交时间", "父记录",
];

function parseArgs(argv) {
  const args = {};
  for (let i = 0; i < argv.length; i += 1) {
    const token = argv[i];
    if (!token.startsWith("--")) continue;
    const key = token.slice(2);
    if (key === "help") { args.help = true; continue; }
    const value = argv[i + 1];
    if (value === undefined || value.startsWith("--")) throw new Error(`参数缺少值: --${key}`);
    args[key] = value;
    i += 1;
  }
  return args;
}

function usage() {
  return [
    "用法: node update-delivery.mjs --delivery 输入.xlsx --score 评分.json --locator 定位.json --prompt 提示词.txt --output 输出.xlsx",
    "可选: --profile 项目画像.json --config delivery-config.json --project-id 项目ID --submitter 提交人 --parent 父PromptID --sheet 工作表",
  ].join("\n");
}

async function readJson(file) {
  return JSON.parse(await fs.readFile(file, "utf8"));
}

function columnName(index) {
  let n = index + 1;
  let out = "";
  while (n > 0) {
    const rem = (n - 1) % 26;
    out = String.fromCharCode(65 + rem) + out;
    n = Math.floor((n - 1) / 26);
  }
  return out;
}

function asText(value) {
  if (value === null || value === undefined) return "";
  return String(value);
}

function getField(source, ...names) {
  for (const name of names) {
    if (source && source[name] !== undefined && source[name] !== null) return source[name];
  }
  return "";
}

// 兼容 round-score.json 模板的嵌套结构（scores.* + comments.*），同时保留扁平中文 key
const DIMENSION_MAP = {
  "交付完整性": ["scores", "delivery"],
  "交付完整性 - 描述": ["comments", "delivery"],
  "指令遵循": ["scores", "instruction"],
  "指令遵循 - 描述": ["comments", "instruction"],
  "任务规划": ["scores", "planning"],
  "任务规划 - 描述": ["comments", "planning"],
  "推理能力": ["scores", "reasoning"],
  "推理能力 - 描述": ["comments", "reasoning"],
  "执行能力": ["scores", "execution"],
  "执行能力 - 描述": ["comments", "execution"],
  "其他问题": ["comments", "other"],
};

function getScore(score, key) {
  if (score && score[key] !== undefined && score[key] !== null) return score[key];
  const path = DIMENSION_MAP[key];
  if (path) {
    const container = score?.[path[0]];
    if (container && container[path[1]] !== undefined && container[path[1]] !== null) return container[path[1]];
  }
  return "";
}

function toIsoText(value) {
  const date = value ? new Date(value) : new Date();
  return Number.isNaN(date.getTime()) ? new Date().toISOString() : date.toISOString();
}

function flattenProfile(profile) {
  if (!profile) return {};
  return {
    reproducibility: profile.reproducibility || "",
    framework: Array.isArray(profile.languageFramework) ? profile.languageFramework.join(", ") : (profile.languageFramework || ""),
    projectId: profile.projectId || "",
  };
}

// 交付表「语言/框架」只写四个主要项，避免把整套技术栈都堆进去。
function requiredDifficulty(value) {
  const text = String(value ?? "").trim();
  if (!text || text === "中等") return "困难";
  return text;
}

function limitLanguageFramework(value, limit = 4) {
  const text = Array.isArray(value) ? value.join(", ") : String(value ?? "");
  const items = text.split(/[,，、;；/]/).map((item) => item.trim()).filter(Boolean);
  return [...new Set(items)].slice(0, limit).join(", ");
}

function createRow(headers, prompt, locator, score, profile, args, config) {
  const profileFields = flattenProfile(profile);
  const projectId = args["project-id"] || getField(score, "项目", "projectId") || profileFields.projectId;
  const sessionId = getField(locator, "SessionID", "sessionId") || getField(score, "SessionID", "sessionId");
  const promptId = getField(locator, "TurnID/PromptID", "promptId") || getField(score, "TurnID/PromptID", "promptId");
  const parent = args.parent ?? getField(score, "父记录", "parentPromptId") ?? "";
  const languageValue = getField(locator, "语言/框架", "languageFramework") || profileFields.framework;
  const languageText = limitLanguageFramework(languageValue);
  const values = {
    "User Prompt": prompt,
    "SessionID": sessionId,
    "TurnID/PromptID": promptId,
    "轮次": getField(locator, "轮次", "round") || getField(score, "轮次", "round") || args.round || "1",
    "初始环境快照": getField(locator, "初始环境快照", "initialSnapshot") || getField(score, "初始环境快照", "initialSnapshot"),
    "提交地址": getField(locator, "提交地址", "commitUrl", "roundCommitUrl") || getField(score, "提交地址", "commitUrl", "roundCommitUrl"),
    "轨迹文件": getField(locator, "轨迹文件", "tracePath", "traceFile"),
    "环境可复现等级": getField(locator, "环境可复现等级", "reproducibility") || profileFields.reproducibility,
    "Harness": getField(locator, "Harness", "harness") || "Claude Code",
    "Harness 版本": getField(locator, "Harness 版本", "harnessVersion", "version"),
    "操作系统": getField(locator, "操作系统", "os") || "",
    "任务类型": getField(locator, "任务类型", "taskType") || getField(score, "任务类型", "taskType"),
    "任务难度": requiredDifficulty(getField(locator, "任务难度", "difficulty") || getField(score, "任务难度", "difficulty")),
    "语言/框架": languageText,
    "交付完整性": getScore(score, "交付完整性"),
    "交付完整性 - 描述": getScore(score, "交付完整性 - 描述"),
    "指令遵循": getScore(score, "指令遵循"),
    "指令遵循 - 描述": getScore(score, "指令遵循 - 描述"),
    "任务规划": getScore(score, "任务规划"),
    "任务规划 - 描述": getScore(score, "任务规划 - 描述"),
    "推理能力": getScore(score, "推理能力"),
    "推理能力 - 描述": getScore(score, "推理能力 - 描述"),
    "执行能力": getScore(score, "执行能力"),
    "执行能力 - 描述": getScore(score, "执行能力 - 描述"),
    "其他问题": getScore(score, "其他问题") || "无",
    "提交人": args.submitter || getField(score, "提交人") || "",
    "提交时间": toIsoText(getField(score, "提交时间", "submittedAt") || getField(locator, "轮次结束时间", "endTime")),
    "父记录": parent,
  };
  const configured = config?.defaults || {};
  for (const [key, value] of Object.entries(configured)) {
    if (values[key] === "" || values[key] === null || values[key] === undefined) values[key] = value;
  }
  return headers.map((header) => values[header] ?? "");
}

function findHeaderRow(values) {
  for (let r = 0; r < Math.min(values.length, 10); r += 1) {
    const row = values[r] || [];
    if (row.includes("SessionID") && row.includes("TurnID/PromptID")) return r;
  }
  return -1;
}

function normalize(value) {
  if (value instanceof Date) return value.toISOString();
  return asText(value).trim();
}

function scanForFormulaErrors(values) {
  const errors = [];
  const pattern = /^#(?:REF!|DIV\/0!|VALUE!|NAME\?|N\/A|NUM!|NULL!)/i;
  for (let r = 0; r < values.length; r += 1) {
    for (let c = 0; c < (values[r] || []).length; c += 1) {
      if (pattern.test(asText(values[r][c]))) errors.push(`${columnName(c)}${r + 1}: ${values[r][c]}`);
    }
  }
  return errors;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.help) { console.log(usage()); return; }
  for (const key of ["delivery", "score", "locator", "prompt", "output"]) {
    if (!args[key]) throw new Error(`缺少必填参数: --${key}`);
  }
  const inputPath = path.resolve(args.delivery);
  const outputPath = path.resolve(args.output);
  if (inputPath === outputPath) throw new Error("输出路径不能覆盖输入文件，请另指定 --output");
  if (await fs.stat(outputPath).then(() => true).catch(() => false)) throw new Error(`输出文件已存在，拒绝覆盖: ${outputPath}`);
  const score = await readJson(args.score);
  const locator = await readJson(args.locator);
  const profile = args.profile ? await readJson(args.profile) : null;
  const config = args.config ? await readJson(args.config) : {};
  const prompt = (await fs.readFile(args.prompt, "utf8")).trim();
  if (!prompt) throw new Error("提示词为空");
  const input = await FileBlob.load(inputPath);
  const workbook = await SpreadsheetFile.importXlsx(input);
  const sheet = args.sheet ? workbook.worksheets.getItem(args.sheet) : workbook.worksheets.getItemAt(0);
  if (!sheet) throw new Error("找不到目标工作表");
  const used = sheet.getUsedRange();
  const matrix = used.values || [];
  const headerRow = findHeaderRow(matrix);
  if (headerRow < 0) throw new Error("未找到包含 SessionID 和 TurnID/PromptID 的表头");
  const headerValues = matrix[headerRow].map(asText);
  const configuredHeaders = config.headers || {};
  const canonicalHeaders = DEFAULT_HEADERS;
  const actualHeaders = canonicalHeaders.map((header) => configuredHeaders[header] || header);
  const missing = actualHeaders.filter((header) => !headerValues.includes(header));
  if (missing.length) throw new Error(`交付表缺少字段: ${missing.join(", ")}`);
  const headerIndex = Object.fromEntries(actualHeaders.map((header) => [header, headerValues.indexOf(header)]));
  const row = createRow(canonicalHeaders, prompt, locator, score, profile, args, config);
  const session = normalize(getField(locator, "SessionID", "sessionId") || getField(score, "SessionID", "sessionId"));
  const promptId = normalize(getField(locator, "TurnID/PromptID", "promptId") || getField(score, "TurnID/PromptID", "promptId"));
  const projectId = normalize(args["project-id"] || getField(score, "项目", "projectId") || profile?.projectId || "");
  const sessionCol = headerIndex[actualHeaders[canonicalHeaders.indexOf("SessionID")]] ?? headerValues.indexOf("SessionID");
  const promptCol = headerIndex[actualHeaders[canonicalHeaders.indexOf("TurnID/PromptID")]] ?? headerValues.indexOf("TurnID/PromptID");
  const projectCol = headerValues.indexOf(configuredHeaders.projectId || "项目");
  let duplicateRowIndex = -1;
  for (let r = headerRow + 1; r < matrix.length; r += 1) {
    const existingSession = normalize(matrix[r]?.[sessionCol]);
    const existingPrompt = normalize(matrix[r]?.[promptCol]);
    const existingProject = projectCol >= 0 ? normalize(matrix[r]?.[projectCol]) : projectId;
    if (existingSession === session && existingPrompt === promptId && existingProject === projectId) {
      duplicateRowIndex = r;
      break;
    }
  }
  let parentPromptId = args.parent ?? getField(score, "父记录", "parentPromptId") ?? "";
  if (!parentPromptId) {
    for (let r = matrix.length - 1; r > headerRow; r -= 1) {
      if (normalize(matrix[r]?.[sessionCol]) === session && normalize(matrix[r]?.[promptCol]) && normalize(matrix[r]?.[promptCol]) !== promptId) {
        parentPromptId = normalize(matrix[r][promptCol]);
        break;
      }
    }
  }
  const targetRowIndex = duplicateRowIndex >= 0 ? duplicateRowIndex : matrix.length;
  const width = Math.max(headerValues.length, actualHeaders.length);
  const targetRange = sheet.getRangeByIndexes(targetRowIndex, 0, 1, width);
  if (duplicateRowIndex < 0) {
    const previousRange = sheet.getRangeByIndexes(targetRowIndex - 1, 0, 1, width);
    targetRange.copyFrom(previousRange, "all");
  }
  const outputRow = Array(width).fill("");
  for (let i = 0; i < actualHeaders.length; i += 1) outputRow[headerIndex[actualHeaders[i]]] = row[i];
  const parentCol = headerIndex[actualHeaders[canonicalHeaders.indexOf("父记录")]];
  if (parentCol !== undefined && !outputRow[parentCol]) outputRow[parentCol] = parentPromptId;
  targetRange.values = [outputRow];
  await workbook.recalculate();
  const after = sheet.getUsedRange();
  const afterValues = after.values || [];
  const errors = scanForFormulaErrors(afterValues);
  if (errors.length) throw new Error(`发现公式错误: ${errors.slice(0, 10).join("; ")}`);
  const preview = await workbook.render({ sheetName: sheet.name, autoCrop: "all", scale: 1, format: "png" });
  const outputDir = path.dirname(outputPath);
  await fs.mkdir(outputDir, { recursive: true });
  await fs.writeFile(`${outputPath}.preview.png`, new Uint8Array(await preview.arrayBuffer()));
  const exported = await SpreadsheetFile.exportXlsx(workbook);
  await exported.save(outputPath);
  const reloaded = await SpreadsheetFile.importXlsx(await FileBlob.load(outputPath));
  const checkSheet = reloaded.worksheets.getItem(sheet.name);
  const checkValues = checkSheet.getUsedRange().values || [];
  const checkRow = checkValues.find((candidate, index) =>
    index > headerRow && normalize(candidate?.[sessionCol]) === session && normalize(candidate?.[promptCol]) === promptId
  ) || [];
  if (normalize(checkRow[sessionCol]) !== session || normalize(checkRow[promptCol]) !== promptId) throw new Error("导出回读校验失败");
  console.log(JSON.stringify({ status: duplicateRowIndex >= 0 ? "updated-existing" : "appended", input: inputPath, output: outputPath, sheet: sheet.name, row: targetRowIndex + 1, projectId, sessionId: session, promptId, preview: `${outputPath}.preview.png`, formulaErrors: errors.length }, null, 2));
}

main().catch((error) => { console.error(`update-delivery 失败: ${error.message}`); process.exitCode = 1; });
