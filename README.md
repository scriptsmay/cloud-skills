# cloud-skills

可公开复用的云服务器 / AI 助手运维技能目录。

A public catalog of reusable skills for cloud servers and AI-agent ops.

每个技能独立一个目录，包含 `SKILL.md` 方法文档，以及可直接部署的脚本与配置模板（`references/`）。
Each skill lives in its own directory: a `SKILL.md` playbook plus deployable scripts and config templates under `references/`.

## 技能索引 / Skills

| 技能 | 说明 |
|---|---|
| [muse-vm-bootstrap](muse-vm-bootstrap/) | 易失容器型 VM（如 Muse 宿主 VM）的基建初始化与灾后恢复：`.bak` unit 规范、出站代理与 TLS MITM 处理、幂等 `restore-infra.sh`、看门狗与多通道告警 |

## 使用 / Usage

1. 按目录阅读 `SKILL.md`，先理解前置认知与检查清单。
2. 把 `references/` 下脚本 / 模板里的 `<PLACEHOLDER>` 按自己的环境填写（密钥只放 600 权限的本地文件，不进仓库）。
3. 按文档步骤部署，用检查清单验收。

## 贡献 / Contributing

- 新技能单独一个目录，保持 `SKILL.md` + `references/` 结构。
- **不要提交任何个人信息**：密钥、token、密码、IP 地址、域名、账号 ID 等一律用 `<PLACEHOLDER>` 占位。
- 文档只写技术中立内容，不写事故叙事与个人上下文。

## License

MIT — see [LICENSE](LICENSE).
