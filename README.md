# aisaka_home

[aisaka.cc](https://aisaka.cc) 根域名的导航页，列出各子站点：blog、run、slides、cloud、status、monitor。

纯静态，一个 `index.html`，无构建步骤。桌面端各前缀右对齐在同一条轴线上，`.aisaka.cc` 跟着鼠标或键盘焦点滑到当前行；手机和触屏设备显示为普通列表。

slides 入口用于幻灯片与演示，页面右下角链接到 [TingChieh 的 GitHub 主页](https://github.com/TingChieh)。

```
aisaka_home/
├── index.html
├── fonts/
│   ├── barlow-condensed-600.woff2   # 仅 latin 子集，自托管（Google Fonts 在国内会卡住渲染）
│   └── OFL.txt                      # Barlow 的 SIL Open Font License
└── ops/
    ├── home.caddy                  # 根域名与泛域名导航页
    └── caddy-cloudflare.conf       # systemd 的 DNS 验证配置
```

## 部署

服务器 isvoro-hk 上的 `/var/www/home/`，Caddy 站点 `/etc/caddy/sites.d/home.caddy`：

```
scp index.html isvoro-hk:/var/www/home/
scp fonts/*.woff2 isvoro-hk:/var/www/home/fonts/
```

增删子站点时记得同步改这里。节点用的伪装域名不要放上来。

### 泛域名直连

Cloudflare 的 `*` CNAME 指向 `aisaka.cc`，代理状态设为“仅 DNS”。已有的具体 DNS 记录优先；Caddy 已配置的具体子域名站点也优先，其他一级子域名由导航页兜底，地址栏保留访问的域名。

`ops/home.caddy` 是对应的 Caddy 配置，需要 `dns.providers.cloudflare` 模块。Caddy 通过 DNS 验证自动签发和续期 `*.aisaka.cc` 的公开可信证书，关闭 CDN 后 HTTPS 仍然有效。

DNS 验证使用仅限 `aisaka.cc` 的 Cloudflare API Token，权限为 `Zone / Zone / Read` 和 `Zone / DNS / Edit`。令牌只写入服务器上的 `/etc/caddy/cloudflare.env`（`root:root`、权限 `600`，内容为 `CF_API_TOKEN=...`），不提交到仓库。`ops/caddy-cloudflare.conf` 供 systemd 加载令牌，并移除原启动命令的 `--environ`，避免把令牌打印到日志。

带 Cloudflare DNS 模块的 Caddy 从官方网站下载到 `/usr/local/bin/caddy`，保持与原服务相同版本；发行版的 `/usr/bin/caddy` 保留。部署上述两个配置文件并校验完整 Caddy 配置后，首次加载模块和环境变量需要重启 Caddy。后续仅改页面文件不需要重启；配置变更使用 `systemctl reload caddy`。更新 Caddy 时仍需包含 `dns.providers.cloudflare` 模块。

2026-11-13 已启用：Caddy `v2.11.9` 自动签发了 Let's Encrypt 泛域名证书。根域名、两个随机一级子域名的 HTTPS、字体加载和现有导航中的六个子站均已验证；证书后续由 Caddy 自动续期。页面或配置更新后提交并推送到 GitHub 留存版本，不额外创建服务器备份。
