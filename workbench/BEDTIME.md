# 晚安故事 · 音色睡前故事

晚安故事是知序工作台的生活模块，入口 `/bedtime.html`，复用现有账号、空间和同源 HTTPS。故事正文保存在独立数据对象里；切换朗读音色不会重新搜索或重新加载故事。

工作台入口在“生活”首页、生活专区侧栏及生活模块库中。PC 使用故事列表／阅读区／常驻播放器三栏，手机使用竖屏布局。返回工作台链接为 `/?area=life`。通用上传链接 `/bedtime.html?panel=voices` 可从播放器或音色窗口复制；链接没有账号或空间标识，首次登录后返回上传窗口，优先进入当前账号自己的可编辑空间。指定 `?space=` 的入口必须是当前账号已有权限的空间。记住的空间以 `zhixu:space:<userId>` 保存，不使用旧全局空间键。

云服务未配置时，录音仅在本机解码为 WAV 并试听，不会提交或标记为克隆音色。关闭窗口、换文件、切空间、退出及离页会停止试听并撤销本地预览地址。启用服务后，用户确认授权并创建音色；只有服务器真正返回就绪档案才自动选择该音色，保留原故事正文，点击播放调用云端合成。

当前无需外部 AI 账号即可使用 14 篇完整原创故事、关键词与分类搜索、设备系统朗读、私有收藏、私有播放记录，以及前端睡眠定时、音量渐弱与合成背景音。设备朗读音色和后台播放能力由 Android/iOS 的浏览器提供，锁屏、切换应用或浏览器暂停时不能保证持续播放；网页音量渐弱不等同于可控制系统朗读音量。手机页面适配并不代表已制作原生 Android/iOS 安装包。

个性化故事生成、云端音色克隆、云端多音色朗读和联网检索使用真实接口适配。没有密钥时不会请求云服务，不生成演示克隆音色，不把设备系统朗读称作本人克隆。管理员在服务器私有环境中配置账号、模型与密钥，并按功能核对提供商额度及声音素材的使用授权；本地模拟测试不能替代真实提供商验收。

## 暖光讲解与故事准备

生活首页提供“睡前小故事”卡片。选择年龄、六类故事、约 1／5 分钟，以及风格、主角、道理、恐怖情节屏蔽和互动偏好；生成成功先展示完整故事，再由用户点击朗读。原创库保留原有故事 ID，增加儿童睡前、治愈温柔、小动物、公主冒险、成长勇气、亲情暖心标签与完整短／长篇；时长按约 250 字／分钟估算，随实际语速变化。年龄筛选只包含明确标注年龄的儿童故事，不把未分级的成人故事当成儿童分级结果。

阅读可逐段显示或展开全文；朗读按句分块、高亮并自动换段。中途温和问题会暂停朗读，用户可直接继续或关闭互动，不要求回答。完整正文只包含一次固定安抚结尾，复制、收藏、历史和主动离线保存仍包含全文。收藏与最近记录可继续打开；切换音色不重新生成或检索故事。

奶黄／豆沙／薄荷暖光界面使用 24px 卡片圆角、棕灰文字和本地 `Zhixu Sleep Sans` 400／500 字重。它是保留 OFL 许可的 Noto Sans SC 字体子集，约 1.73 MB，覆盖 GB2312 汉字，其他生僻字回退设备字体；许可和来源见 `public/fonts/README.md`。夜间使用更暗暖色，正文和控件字号增加 10%；减少动态效果偏好会关闭呼吸动效。钢琴音和白噪音由 WebAudio 合成，不下载外部曲目。

定时支持 30／60／90 分钟以及较短选项，最后一分钟可渐弱。到期停止朗读、背景音和录音试听，关闭音频上下文并进入可唤醒的低亮休眠页。退出、换账号或撤权清理正文、问题、音色、计时及休眠状态；后台暂停时只能在恢复页面后核对真实截止时间。

## 服务端配置

仅在明确准备启用云服务时填写。密钥不得写入 Git、浏览器、发布包或共享空间记录。

| 变量 | 用途与默认值 |
| --- | --- |
| `DASHSCOPE_API_KEY` | 阿里云百炼中国内地密钥；空值关闭 AI 故事生成、克隆和云端朗读 |
| `STORY_GENERATION_MODEL` | 默认 `qwen-plus`；可明确选择 `qwen3.8-flash`。其他值关闭生成，不回退 |
| `STORY_GENERATION_ENABLED` | 默认 `1` 兼容已有配置；只有 `1` 允许生成，设 `0` 可独立关闭 |
| `CLOUD_VOICE_ENABLED` | 默认 `1` 兼容已有配置；只有 `1` 允许克隆及云端合成。先只启用故事时设 `0`，仍可使用设备朗读 |
| `CLOUD_VOICE_MODEL` | 默认保留旧 `qwen3-tts-vc-2026-01-22` 配置；可明确选择 `qwen-audio-3.1-tts-flash`，注册和合成均绑定该模型。未知值关闭语音，不回退 |
| `CLOUD_VOICE_API_HOST` | 新语音接口所需的北京业务空间 API Host，来自百炼业务空间管理；仅允许 `xxx.cn-beijing.maas.aliyuncs.com` 的 HTTPS origin，无路径、查询或凭据。不是 API Key |
| `STORY_SEARCH_PROVIDER` | `local` 默认原创库；可选 `opensearch` 或 `tavily` |
| `OPENSEARCH_API_KEY` | 阿里云 OpenSearch AI 搜索服务专用密钥，与百炼密钥独立 |
| `OPENSEARCH_ENDPOINT` | 控制台提供的 HTTPS 公网服务 origin，主机须属于 `*.opensearch.aliyuncs.com`；不填路径 |
| `OPENSEARCH_WORKSPACE` | OpenSearch workspace 名称，默认 `default` |
| `TAVILY_API_KEY` | 仅在选用 Tavily 时填写；中国大陆网络可达性未验证 |

分阶段接入时，先在百炼北京地域核对故事模型的实际余额和有效期，开启该模型“免费额度用完即停”，再配置 `STORY_GENERATION_MODEL=qwen3.8-flash`、`STORY_GENERATION_ENABLED=1`、`CLOUD_VOICE_ENABLED=0` 及私有 Key。新语音路线先核对 `qwen-audio-3.1-tts-flash` 的有效免费额度并开启用完即停，再明确设置 `CLOUD_VOICE_MODEL`、业务空间 `CLOUD_VOICE_API_HOST`，最后启用语音。Key 保留故事模型权限，添加该语音模型；若模型列表可选择 `voice-enrollment`，同时添加注册服务，不能因授权失败自动开放全部模型。应用开关控制是否请求供应商，不能替代供应商的额度停止机制。

新版 `voice-enrollment` 创建 Qwen Audio 音色当前免费，云端朗读仍按合成模型用量计费，免费额度有有效期。旧 `qwen-voice-enrollment` 注册音色单独计费，不能用朗读额度抵扣，也不会作为新版失败时的后备服务。依据：[模型价格](https://help.aliyun.com/zh/model-studio/model-pricing)、[免费额度规则](https://help.aliyun.com/zh/model-studio/new-free-quota)。

回退到尚未支持新版语音配置的应用版本前，先设置 `CLOUD_VOICE_ENABLED=0`。恢复旧应用时保留数据库、媒体与私有密钥，避免旧版本忽略新模型配置后启用旧语音路线。

配置有效只表示具备调用条件，并不表示提供商验证、免费额度或联网可达性已确认。云服务存在用量费用；应用不会创建任何云付费资源，也不自动填入 API 密钥。

故事生成使用固定百炼兼容接口 `https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions` ，按服务端配置使用 `qwen-plus` 或 `qwen3.8-flash`。后者显式关闭思考并以 `max_completion_tokens` 限制输出；前者保持原有请求参数。模型只由管理员环境配置选择，浏览器偏好不能指定模型或接口；配置不支持的模型时关闭生成，不切换其他可能计费的模型。只发送经过枚举校验的偏好，不发送姓名或已有笔记。每账号最多 10 次／小时、20 次／日，并与音色服务共用最多 2 个并发请求。服务器校验 JSON 段落、长度和温和问题，按默认开启的恐怖词检查拒绝明显不合适输出；这不是内容适龄的绝对保证。模型失败、被截断或输出不符合设置时返回明确错误，不提供伪造生成结果。生成文本不自动写数据库或缓存；主动收藏／播放记录时才保存私人快照，普通账号导入不能冒充内置或官方生成来源。

浏览器将克隆样本解码并导出为 **24 kHz、单声道、16 位 PCM WAV**；服务器再次检查实际 10–30 秒时长、完整帧数据、非空声音，最大 1.5 MB、JSON 上限 2.5 MB。用户必须确认声音为本人或已获授权。每个空间每个账号最多 10 个档案，模型 ID 与供应商音色 ID 仅服务端保存。

新版使用 `voice-enrollment` 的 `create_voice` 与 `query_voice` 接口，绑定 `qwen-audio-3.1-tts-flash`。提供商需要能读取的样本 URL：服务端将录音放入现有私有 S3，生成最长 300 秒的只读签名 URL，仅交给百炼，查询状态为 `OK` 后才标为就绪。录音不创建可供应用读取的音频记录，签名 URL 不返回浏览器；成功或失败均把临时样本送入持久删除队列并尝试清理，队列积压或存储故障时稍后重试，进程崩溃遗留的预约在 1 小时后回收。未完成删除的对象仍计入存储容量，签名链接在期限后失效。普通媒体访问仍经过账号与空间鉴权，存储桶保持私有。新版克隆需要 S3，只有本地存储时明确关闭克隆。

旧配置仍保留 `qwen-voice-enrollment` 与 `qwen3-tts-vc-2026-01-22` 的 Base64 请求，便于已有安装继续使用。切换模型不会迁移已有音色，也不把旧音色发给新合成模型。新接口见 [音色创建](https://help.aliyun.com/zh/model-studio/voice-clone-design-http-api) 与 [HTTP 合成](https://help.aliyun.com/zh/model-studio/qwen-audio-tts-http-api)。

新版云端系统声音与克隆声音使用同一合成模型，系统选项包括 `wenhuaiqing_v3.1`、`longyuan_v3.1`、`longsanshu_v3.1`；旧配置仍使用 `qwen3-tts-flash` 系统音色。克隆音色必须使用创建时的目标模型，不能跨模型调用。应用每次合成最多 600 字，前端按句分段顺序播放；这是应用限制，不是供应商公布的模型上限。语速通过播放器的 `playbackRate` 调整，不向提供商传不存在的速度参数。切换音色会取消旧的前端播放结果，保留已选文本；云端生成和网络传输需要时间，不保证零延迟切换。

云声音服务最多 2 个并发请求；每账号克隆最多 5 次/小时、10 次/日；合成最多 60 次/分钟、100 次/日，每次最多 600 字，因此输入最多 60,000 字/日。这些是应用防滥用上限，**不是云供应商免费额度**。供应商错误响应、签名 URL、密钥和样本不会返回浏览器。服务端仅下载官方结果 OSS 主机下的音频，拒绝跨主机重定向；文档中的 HTTP 结果链接只对该白名单主机升级 HTTPS。音频缓存 1 小时，GET 始终校验用户及空间，禁止共享成员读取他人的合成音频。

删除已经就绪的音色会先请求提供商删除音色，成功后删除本地档案。若密钥缺失或供应商删除失败，保留本地档案并提示重试，避免界面声称云端已删除。创建过程中退出账号或被移除空间时，结果不会保存或返回，后台会尽力清理刚生成的供应商音色。网络失败时仍可能需要管理员到提供商控制台核对残留音色；百炼规则可能在一年未使用后自动清理音色。

## 文本检索与离线边界

原创库为本项目自行创作，来源明示为“知序原创故事”。联网检索直接调用受配置的 OpenSearch 或固定 Tavily API，不请求用户提交的网页 URL，不允许任意内网目标或供应商端点重定向。搜索服务返回的正文可能不完整、包含网页附加内容，且不保证每个结果都有正文；摘要不等同于全文。结果保持原始来源链接与版权说明、`isExcerpt: true`，正文为空时不得直接当完整故事朗读。网页内容版权仍归原作者，保存、复制和离线下载前应确认使用权。

收藏与播放记录按 `space_id + user_id` 隔离；共享空间的其他成员看不到这些私人记录、克隆音色或音频。`viewer` 可阅读原创故事及自己的已有记录，不能修改收藏、历史、音色或调用云合成。每账号每空间最多 200 篇收藏，最近 200 条不同故事播放记录；收藏可主动保存网页检索文本或自有文本。用户可主动把选中的故事正文保存到当前设备缓存，也可下载为 TXT 文件；缓存按账号与空间分隔，在退出账号或检测到权限撤销时清理。浏览器不后台缓存私密 API、账号凭据、密码箱或音频。已打开的页面可读取主动保存的正文，但当前不保证断网后重新打开完整应用或重新验证登录。

## 接口约定

所有接口以 `/api/spaces/:spaceId/bedtime/` 开头，需现有会话及空间成员身份。写入要求 owner/editor，现有 `X-Requested-With: Workspace` 和 Origin 检查统一保护。所有 JSON 响应 `Cache-Control: no-store`。

| 方法与路径 | 结果或输入 |
| --- | --- |
| `GET config` | 实际能力开关，`storyGeneration`、系统设备朗读、联网搜索、克隆、云合成；不含密钥 |
| `POST generate` | 输入个性化参数，成功 200 `{story}`；缺密钥 503、只读 403、用量／并发限制 429 |
| `GET search?q=&category=&scope=local\|web&durationMinutes=1\|5&ageGroup=3-6\|7-10` | `{stories,scope,query}`；分类与时长独立于音色，年龄／时长参数可省略；`web` 无配置返回 503 |
| `GET stories/:storyId` | `{story}`，原创或当前用户已收藏故事 |
| `GET favorites` | `{favorites}`，包含完整保存正文与 `favorited_at` |
| `PUT favorites/:storyId` | 原创可传 `{}`；导入需 `{story:{title,text,category,source}}` |
| `DELETE favorites/:storyId` | 删除当前用户收藏 |
| `GET history` | `{history}`，含文本快照、`voiceId`、`positionSeconds`、`playedAt` |
| `POST history` | `{storyId,voiceId,positionSeconds,story?}`；未收藏导入故事需带 `story` 正文 |
| `DELETE history` | 清除当前用户记录 |
| `GET voices` | `{voices,systemVoices}`；仅返回当前用户克隆档案和可用系统云音色 |
| `POST voices` | `{name,mimeType:"audio/wav",audioBase64,consent:true}`；成功返回 `{voice}` 与 201 |
| `DELETE voices/:voiceId` | 删除本人档案与供应商音色 |
| `POST synthesize` | `{text,voiceId}`；最多 600 字；返回同源私有 `{audioUrl,mimeType,expiresIn}` |
| `GET audio/:audioId` | 仅本人可读的 WAV；1 小时到期后需重新生成 |

生成请求示例（使用现有已登录会话及同源写入头）：

```json
{"ageGroup":"3-6","category":"小动物","durationMinutes":1,"style":"healing","moral":true,"blockScary":true,"protagonist":"rabbit","interactive":true}
```

`ageGroup` 为 `3-6`／`7-10`，`durationMinutes` 为整数 `1`／`5`，`style` 为 `healing`／`fantasy`／`realistic`，`protagonist` 为 `cat`／`rabbit`／`child`，其余开关必须是布尔值。响应故事提供 `id/title/text/category/tags/readMinutes/paragraphs/questions/ending/generationParameters/source`；`paragraphs` 仅含正文段落，`text` 已包含 `ending`，不能再次追加。`questions` 中 `afterParagraph` 是从零开始的正文段落索引。

## 持久化、迁移与验证

`bedtime.SCHEMA` 只新增 `bedtime_favorites`、`bedtime_history`、`bedtime_voices`、`bedtime_audio` 和索引，不修改既有表。用户或空间删除时新表通过外键级联删除。数据库和 `data/bedtime-audio` 必须随现有持久化数据卷备份；旧程序可以忽略新增表，应用回退兼容，但供应商已经执行的调用和计费不随镜像回退。音频与普通媒体共享容量限制和 100 MiB 磁盘余量检查。合成缓存按请求及启动清理到期条目，不能以此代替正式备份策略。

`python -m unittest discover -s tests -v` 包含真实 HTTP 权限及隔离测试，云服务使用显式注入的假提供商验证协议和授权竞态，不会触发真实计费。覆盖共享成员隔离、只读角色、实际样本时长和 30 秒边界、大 Base64、音色切换保持文本、供应商 ID 不外泄、合成音频鉴权、退出登录/成员移除期间的结果丢弃、失败不假报完成、并发限制、磁盘余量、固定音频主机与重定向拒绝、官方裸 PCM 包装为 WAV。通过本地测试不代表已经公网部署或云供应商实测。

官方资料：[音色复刻](https://help.aliyun.com/zh/model-studio/voice-cloning-user-guide)、[Qwen-TTS HTTP API](https://help.aliyun.com/zh/model-studio/qwen-tts-api)、[音色复刻 HTTP API](https://help.aliyun.com/zh/model-studio/voice-clone-design-http-api)、[系统音色列表](https://help.aliyun.com/zh/model-studio/qwen-tts-voice-list)、[Qwen3.8-Flash](https://help.aliyun.com/zh/model-studio/qwen3-8-flash)、[结构化输出](https://help.aliyun.com/zh/model-studio/qwen-structured-output)、[免费额度停止规则](https://help.aliyun.com/zh/model-studio/new-free-quota)、[OpenSearch 网页搜索](https://help.aliyun.com/zh/open-search/search-platform/developer-reference/web-search)、[OpenSearch 公网服务地址](https://help.aliyun.com/zh/open-search/search-platform/user-guide/get-service-call-address)。
