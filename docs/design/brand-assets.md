# 品牌标志说明（开源版的中性标）

> 给后来人的一页说明：现在挂的是什么标志、那 9 张图是干什么的、怎么换成你自己的标。
> 界面口径仍以 `docs/design/design-system-v1.md` 为准，本文只管「标志」这一件事。

## 一、现在用的是什么

开源版**不带任何客户 / 公司品牌视觉**。顶栏与登录页用的是中性占位标——一个圆角方块加三条白色圆角短条，纯黑 `#000000`、透明底，形状不与任何已知品牌对应：

| 文件 | 画布 | 渲染尺寸 | 用在哪 |
|---|---|---|---|
| `app/oams/static/brand/logo.png` | 730×570（RGBA） | **38×30**（模板只写 `height="30"`，宽度按比例） | 顶栏，`app/oams/templates/base.html:822` 的品牌位 |
| `app/oams/static/brand/logo-login.png` | 369×288（RGBA） | **77×60**（模板写 `width="77" height="60"`） | 登录页，`app/oams/templates/login.html:11` 的卡片顶部 |

两个文件都只有几 KB（15545 B / 7480 B），8× 超采样后降采样，没有阴影、没有描边。

**一句话**：图标是占位物，不是品牌资产。要换成你自己的标，走第三节；原来那套品牌素材的去处见第四节。

## 二、图在哪：`assets/screenshots/brand-neutral/`

这 9 张是「换上中性标之后」的三档回归截图，**自检证据，不是产物**——用来证明换标没把布局搞坏（顶栏没被撑高、登录卡片没被顶开、窄屏不横滚）。

| 文件 | 页面 | 宽度档 | 体积 |
|---|---|---|---|
| `assets/screenshots/brand-neutral/1440-login.png` | 登录页 `/login` | 1440 | 36677 B |
| `assets/screenshots/brand-neutral/1024-login.png` | 登录页 `/login` | 1024 | 34303 B |
| `assets/screenshots/brand-neutral/768-login.png` | 登录页 `/login` | 768 | 32623 B |
| `assets/screenshots/brand-neutral/1440-newpage.png` | 新建部门页面 `/departments/{HQ}/new` | 1440 | 77752 B |
| `assets/screenshots/brand-neutral/1024-newpage.png` | 新建部门页面 `/departments/{HQ}/new` | 1024 | 73353 B |
| `assets/screenshots/brand-neutral/768-newpage.png` | 新建部门页面 `/departments/{HQ}/new` | 768 | 69861 B |
| `assets/screenshots/brand-neutral/1440-settings.png` | 设置页 `/settings` | 1440 | 101237 B |
| `assets/screenshots/brand-neutral/1024-settings.png` | 设置页 `/settings` | 1024 | 96543 B |
| `assets/screenshots/brand-neutral/768-settings.png` | 设置页 `/settings` | 768 | 89342 B |

当时的实测结论（三档 × 三页 = 9 页次）：`documentElement.scrollWidth - clientWidth = 0`、越界元素 `offenders: []`、顶栏标志 38×30、登录页标志 77×60 三档都不变。

**为什么不拍看板 / 进度中心 / 审核页**：那三页的截图属于另一批（`assets/screenshots/spacing-sweep/`、`assets/screenshots/dead-css/` 等），它们证明的是间距与栅格，与本页无关；换标只影响顶栏和登录页两处，其余页面与顶栏共用同一个 `base.html` 品牌位，标识一处就够。

## 三、怎么换成你自己的标

**只替换两个图片文件，模板一行都不用改。**

```sh
# 把你自己的两个文件按同名同路径放进去（示例，路径按你的实际来）
cp /path/to/你的-logo.png       app/oams/static/brand/logo.png
cp /path/to/你的-logo-login.png app/oams/static/brand/logo-login.png
```

要点：

1. **模板不依赖画布尺寸**。模板里只写显示尺寸——顶栏 `app/oams/templates/base.html:822` 只写 `height="30"`（宽度按原图比例自动算出 38），登录页 `login.html:11` 写 `width="77" height="60"`——都不写图片本身的像素数；换上去的图只要**宽高比与原图接近**（顶栏 730×570 ≈ 1.28:1，登录页 369×288 ≈ 1.28:1），就不会把布局撑开。比例差得多也不会破版——`<img>` 按 `width`/`height` 属性缩放，只会略微变形或留白。
2. **缓存不用手动清**。模板引用走 `static_url('brand/logo.png')`，链接上带内容哈希（形如 `?v=10409f3f`）——**换图后哈希自动变**，浏览器会当成新地址重新取，不会吃到旧图。这也是上一轮把静态引用全部改成 `static_url()` 的收益之一。
3. **不需要改 `_VersionedStaticFiles` 或任何后端配置**：哈希由 `app/oams/web.py` 的 `static_url()` 现算，版本串对不上时服务端回 `no-cache`，对得上才回 `immutable`。
4. 换完起服务看一眼顶栏与登录页即可；要留证据就按第二节的三档重新截图（ego 三档，断言 `scrollWidth === clientWidth`）。

## 四、原品牌素材：不在本仓，也不该进来

原来那套公司品牌素材（`.ai` 源文件 + `out/`、`ref/`、`svg/` 生成物，合计约 1.9 MB）**已经移出仓库**；现在仓库里与品牌有关的，只有第一节那两个中性占位标。

- **它不在仓库里**：仓库的 `.gitignore` 排除了本地备份类目录，`git ls-files` 查不到它，公开历史里也没有它。
- **它也不该进仓库**：品牌源属于公司资产，进公开仓等于把商标连同源文件一起发出去；真要纳入版本管理，该放公司自己的私有仓，不是这个开源仓。
- **贡献者不用找它**：本仓的视觉只用那两个占位标；要换成自己公司的标，按第三节替换文件即可，不需要这套源文件。

## 五、本页的自检命令

```sh
# 1) 本页提到的 9 张图，路径全部真实存在，且与入库文件数一致
grep -o 'assets/screenshots/brand-neutral/[0-9a-z-]*\.png' docs/design/brand-assets.md | sort -u | wc -l   # → 9
git ls-files assets/screenshots/brand-neutral | wc -l                                                        # → 9
grep -o 'assets/screenshots/brand-neutral/[0-9a-z-]*\.png' docs/design/brand-assets.md | sort -u | xargs ls -l | wc -l  # → 9（有任何一个不存在会报错）

# 2) 两个标志文件在位
ls -l app/oams/static/brand/logo.png app/oams/static/brand/logo-login.png

# 3) 模板里没有一行按画布尺寸算布局（只有显示尺寸属性）
grep -rn "brand/logo" app/oams/templates/base.html app/oams/templates/login.html
```
