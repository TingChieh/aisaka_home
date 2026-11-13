# aisaka_home

[aisaka.cc](https://aisaka.cc) 根域名的导航页，列出各子站点：blog、run、slides、cloud、status、monitor。

纯静态，一个 `index.html`，无构建步骤。桌面端各前缀右对齐在同一条轴线上，`.aisaka.cc` 跟着鼠标或键盘焦点滑到当前行；手机和触屏设备显示为普通列表。

slides 入口用于幻灯片与演示，页面右下角链接到 [TingChieh 的 GitHub 主页](https://github.com/TingChieh)。

```
aisaka_home/
├── index.html
└── fonts/
    ├── barlow-condensed-600.woff2   # 仅 latin 子集，自托管（Google Fonts 在国内会卡住渲染）
    └── OFL.txt                      # Barlow 的 SIL Open Font License
```

## 部署

服务器 isvoro-hk 上的 `/var/www/home/`，Caddy 站点 `/etc/caddy/sites.d/home.caddy`：

```
scp index.html isvoro-hk:/var/www/home/
scp fonts/*.woff2 isvoro-hk:/var/www/home/fonts/
```

增删子站点时记得同步改这里。节点用的伪装域名不要放上来。
