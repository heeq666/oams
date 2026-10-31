# 品牌标志说明（开源版的中性标）

> 给后来人的一页说明：现在挂的是什么标志、那 9 张图是干什么的、怎么换回内部版、东西存在哪。
> 界面口径仍以 `docs/design/design-system-v1.md` 为准，本文只管「标志」这一件事。

## 一、现在用的是什么

开源版**不带任何客户 / 公司品牌视觉**。顶栏与登录页用的是中性占位标——一个圆角方块加三条白色圆角短条，纯黑 `#000000`、透明底，形状不与任何已知品牌对应：

| 文件 | 画布 | 渲染尺寸 | 用在哪 |
|---|---|---|---|
| `app/workhub/static/brand/logo.png` | 730×570（RGBA） | **38×30**（模板只写 `height="30"`，宽度按比例） | 顶栏，`app/workhub/templates/base.html:822` 的品牌位 |
| `app/workhub/static/brand/logo-login.png` | 369×288（RGBA） | **77×60**（模板写 `width="77" height="60"`） | 登录页，`app/workhub/templates/login.html:11` 的卡片顶部 |

两个文件都只有几 KB（15545 B / 7480 B），8× 超采样后降采样，没有阴影、没有描边。

**一句话**：图标是占位物，不是品牌资产。要换成公司自己的标志，走第三节；要换回内部版，走第四节的备份。

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

## 三、怎么换回内部版（或换成任意自己的标）

**只替换两个图片文件，模板一行都不用改。**

```sh
# 把内部版的两个文件按同名同路径放回去（示例，路径按你的实际来）
cp /path/to/你的-logo.png       app/workhub/static/brand/logo.png
cp /path/to/你的-logo-login.png app/workhub/static/brand/logo-login.png
```

要点：

1. **模板不依赖画布尺寸**。模板里只写显示尺寸——顶栏 `app/workhub/templates/base.html:822` 只写 `height="30"`（宽度按原图比例自动算出 38），登录页 `login.html:11` 写 `width="77" height="60"`——都不写图片本身的像素数；换上去的图只要**宽高比与原图接近**（顶栏 730×570 ≈ 1.28:1，登录页 369×288 ≈ 1.28:1），就不会把布局撑开。比例差得多也不会破版——`<img>` 按 `width`/`height` 属性缩放，只会略微变形或留白。
2. **缓存不用手动清**。模板引用走 `static_url('brand/logo.png')`，链接上带内容哈希（形如 `?v=10409f3f`）——**换图后哈希自动变**，浏览器会当成新地址重新取，不会吃到旧图。这也是上一轮把静态引用全部改成 `static_url()` 的收益之一。
3. **不需要改 `_VersionedStaticFiles` 或任何后端配置**：哈希由 `app/workhub/web.py` 的 `static_url()` 现算，版本串对不上时服务端回 `no-cache`，对得上才回 `immutable`。
4. 换完起服务看一眼顶栏与登录页即可；要留证据就按第二节的三档重新截图（ego 三档，断言 `scrollWidth === clientWidth`）。

## 四、回滚：原品牌源存在哪

原来那套公司品牌素材（`.ai` 源文件 + `out/`、`ref/`、`svg/` 生成物，合计约 1.9 MB）**已经移出仓库**，落在本地备份目录：

```
.local-backups/brand-source-20261101/2026<原品牌>新logo.ai   # 1,412,944 B（文件名里的品牌名此处略去）
.local-backups/brand-source-20261101/{out,ref,svg}/              # 导出物与脚本产物
```

> 目录里那个 `.ai` 的真实文件名带着客户品牌名，这里用 `<原品牌>` 占位——本页是要开源的，不能为了说明「东西存在哪」把它写回来。

- **它不在仓库里**：`.gitignore:29` 覆盖了整个 `.local-backups/`，`git ls-files` 查不到它，开源仓的历史里也没有它。
- **它也不该进仓库**：品牌源属于公司资产，进公开仓就等于把商标连同源文件一起发出去；将来真要把它放回版本管理，应该放公司自己的私有仓，不是这个开源仓。
- 换回内部版的做法见第三节——把两个图片文件换掉即可，不必把品牌源搬回来。
- 若本机没有这个备份目录（换机器、清理过），内部版的原始标志需要向品牌/市场那边重新要，代码侧的换法不变。

## 五、本页的自检命令

```sh
# 1) 本页提到的 9 张图，路径全部真实存在，且与入库文件数一致
grep -o 'assets/screenshots/brand-neutral/[0-9a-z-]*\.png' docs/design/brand-assets.md | sort -u | wc -l   # → 9
git ls-files assets/screenshots/brand-neutral | wc -l                                                        # → 9
grep -o 'assets/screenshots/brand-neutral/[0-9a-z-]*\.png' docs/design/brand-assets.md | sort -u | xargs ls -l | wc -l  # → 9（有任何一个不存在会报错）

# 2) 两个标志文件在位
ls -l app/workhub/static/brand/logo.png app/workhub/static/brand/logo-login.png

# 3) 模板里没有一行按画布尺寸算布局（只有显示尺寸属性）
grep -rn "brand/logo" app/workhub/templates/base.html app/workhub/templates/login.html
```
