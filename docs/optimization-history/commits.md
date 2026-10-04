# 开发提交索引

整理日期：2026-10-04。原开发仓库截至 `59327cb54c909d5c3153e1171421b4f4890ebcb0` 共 **104 个提交**，下面全部列出，包括功能、修复、测试、实验归档和文档调整。时间由 Git 提交时间换算为北京时间（UTC+8），顺序使用 `git log --reverse`。

这些提交属于本地开发仓库，未将其 Git 对象推送到公开仓库，因此 SHA 使用代码形式展示，不能当作公开仓库的 commit 链接。索引是版本元数据，公开实现从干净快照起步；未保存为提交的中间编辑和每次工具调用不在此表中。09-16—09-21 的早期迭代见[阶段记录](README.md)。

## 原开发仓库

### 2026-09-23

| 时间 | 本地提交 SHA | 当时保存的变更摘要 |
| --- | --- | --- |
| 17:08:37 | `5ec0b27711deb57f95390ae1a0d941758605ca94` | chore: checkpoint ResearchAgent V3 and resume evidence |
| 17:14:53 | `9e5c093142d79521bac9048c2b3a649d551323ab` | docs: prioritize V4 controlled experiment loop |
| 18:33:09 | `2a57f73b9f2461d3b45ff526c79990c94fb3089e` | fix: bind list citations and resume saved verification |
| 20:28:02 | `b1650ec039b82ab5740add493575f4adcf269cf0` | feat: add controlled V4 coding experiment cycle with real acceptance |
| 22:47:22 | `32956f4747a1c6235a013f802abbc1213f2ada01` | fix(v4): add development feedback and correct experiment evaluation |

### 2026-09-25

| 时间 | 本地提交 SHA | 当时保存的变更摘要 |
| --- | --- | --- |
| 01:54:12 | `5aa4b49eddfec77c11aa82623dec17f9fa0686e6` | fix: recover V4 ranking quality with evidence reranking and development selection |

### 2026-09-27

| 时间 | 本地提交 SHA | 当时保存的变更摘要 |
| --- | --- | --- |
| 00:08:17 | `8cba1f4b5059ec24050f5ecb1a4701375d9189f5` | docs: record incomplete iteration at requested deadline |
| 03:08:57 | `b3b2dd90561fcb80c3c7dfa1b9d9c07e59b4e7b7` | feat: add evidence-aware QA and bounded coding experiments |
| 21:00:05 | `a7a4a3861ae733fa5eecdc2cd8319a81082fe897` | chore: organize evaluation datasets and provenance |

### 2026-09-28

| 时间 | 本地提交 SHA | 当时保存的变更摘要 |
| --- | --- | --- |
| 22:13:20 | `9189511177f2849dbc417bede0b8a565a98a60e9` | feat: add reproducible mixed benchmark suite |

### 2026-09-29

| 时间 | 本地提交 SHA | 当时保存的变更摘要 |
| --- | --- | --- |
| 01:42:31 | `c289f603f98aef5e5ed6c7d01be9043d1cc1db15` | fix: evaluate actual retrieval and agent workflows |
| 21:37:13 | `16568324b9c0b18bd56d64124aa0607bf5ddd506` | Implement optional Auto Research tools and validate first real research workflow |
| 22:53:38 | `0a0a66df28314535ca2daf234e255d0ce7920a92` | Improve bounded evidence reading and verification; update resume and goal status |

### 2026-09-30

| 时间 | 本地提交 SHA | 当时保存的变更摘要 |
| --- | --- | --- |
| 00:36:54 | `23caf07b66f78e3eb6f1b2c5c55967874940268a` | Improve experiment feedback provenance and record r14 research evidence |
| 01:46:39 | `e0ec65a3f5274ce0b954a8294d8724bae0d50df8` | docs: publish researchagent resume r16 |
| 01:48:48 | `471d68ecb8409d143406f8dcd70a6e3a8d021145` | docs: align handoff with resume r16 |
| 02:19:33 | `5beddf1f820409eef39bc7d989f1ce207f88c81f` | feat: archive auto research measurements in V3 |
| 02:29:05 | `39b7ef98eeab2a581b51ed89cfcb124dfe4bebed` | docs: publish researchagent resume r18 |
| 03:12:43 | `fd3b19327bf7fbbd3f7c035f3195673ad642cd7b` | docs: publish researchagent resume r20 |
| 03:17:53 | `3d9c639016b81b3ca9b16b15b854868626285a95` | feat: add host managed experiment model interface |
| 04:29:17 | `4a42d3e19b00b6b86dfb0043258283963f5c2bb4` | docs: publish researchagent resume r21 |
| 04:32:07 | `068a6834bb5e05c211aa21b8523246f29f246e34` | docs: record auto research r2 boundary |
| 04:45:33 | `28b418bf7180e4e3a879631571cbaed0c8e622ed` | fix: bound auto research feedback and detect inspect loops |
| 05:10:46 | `9a054f3a18ec36df7f02787e40f9eb7045d20f72` | docs: publish researchagent resume r22 |
| 05:32:52 | `2ab2690393b03086c5e77e2d14f68648ac7ee44d` | fix: bound search context and allow generated-code repair |
| 05:37:45 | `dc32ae1a292f28c94dc641b85170cdaa05a63731` | docs: publish researchagent resume r23 |
| 06:53:59 | `9a3f958ec3d1e5e4c00a666bb72314ca0aef7590` | fix: compare structured experiment conditions |
| 07:21:35 | `ff3ce02f56b7f251bbc02338f43db39cc2b9f8d3` | docs: publish researchagent resume r24 |
| 07:25:06 | `167e1ba94ce9971ef9ebb5fbcc50011f46e6f638` | fix: allow cross-round generated source revisions |
| 08:11:44 | `ac59aa30fd6d76c3670696924030e5639826e550` | test: cover native cross-round revisions |
| 08:14:16 | `2c7a1c99a00f20656f8e1cb2ae6beda55b85350d` | docs: publish researchagent resume r25 |
| 08:53:02 | `1af41353dd0eb5cba31bf82009eb63188bb3cf8a` | docs: publish researchagent resume r26 |
| 09:30:47 | `26c10f204c918096925d810d326e6750fc54c2e0` | fix: validate cross-round auto research iterations |
| 09:32:23 | `b02e299ececf8c9c45314ebd21ca05e9908cfd1b` | docs: publish researchagent resume r27 |
| 10:42:10 | `9f2174c8b3ac00b1817caffebf6283e10c7f9ef7` | docs: tighten researchagent resume and result assessment |
| 11:57:24 | `ee7ff14137cd9a9a702b017a6901ac63b53825dc` | docs: condense researchagent resume to r30 |
| 16:44:48 | `b362899de7971efa0ac66d13243b295e895bfc3d` | fix: preserve reproducible auto research iterations |
| 17:59:33 | `34bbcb6364d275ba852af04c4f4422b13e269c79` | fix: preserve original evidence and recover research checkpoints |
| 20:12:25 | `5371c6c45ff3ca103b7830675c889eb7133a3fad` | feat: support sequential host model experiments with bounded supervision |
| 20:22:10 | `552051af87631f4d4fbaa597ed6adb5ddb80a846` | docs: balance resume detail and quantified outcomes |
| 21:33:41 | `fc467e8b69c2ac53722d474425dd379add2dc677` | feat: prepare frozen A-MEM research campaign and host scoring |
| 21:58:04 | `84ee70d8b6e0b3970b2fc4f2f44ccfefa5f68c87` | fix: retain citation repair hints and prioritize unchecked evidence |
| 23:16:22 | `6d10a42fdc9351310bebc5eaaa45087aadf6beec` | fix: scope native experiment denials to process lifetime |

### 2026-10-01

| 时间 | 本地提交 SHA | 当时保存的变更摘要 |
| --- | --- | --- |
| 11:58:05 | `fb16d4cb1289ac94a31cb4bb74ceee3dc00d28ca` | fix: preserve prior experiment grants during native cleanup |
| 12:17:16 | `a977422c92a37d8b78d7a0ea360392d11b7abc16` | docs: record authorized A-MEM campaign and recovery evidence |
| 17:36:22 | `a0bd2fd324e9114d7b41511aa08b39911c2967b2` | fix: account for one unknown sequential call after interruption |
| 18:00:43 | `40e2eff2f6d1f95905851c75aee7dffaca5b314c` | docs: preserve authenticated campaign recovery and reading failure evidence |
| 18:20:23 | `11788a67ddcdd5e1a7b8fe0128aa84f74d16b803` | eval: preserve short-ID verifier candidate and live replay evidence |
| 18:49:38 | `ab8537b4350df7e59e8ee946ae81ee5c50d4376f` | eval: preserve library-bound reading recovery and live evidence |
| 19:14:39 | `c4d99c2b13f438dc1e04c19e9a7fa9dda4af7975` | fix: restore evidence recovery scopes and clarify model limits |
| 19:38:57 | `50aab95adaf55784b1f1ae6e1e9d5383c3e7c845` | fix: preserve complete experiment prompts within aggregate limits |
| 19:51:50 | `5558a2f4f727915641c7758e68821f80e2fc7ec5` | eval: continue full A-MEM baseline with bound cache provenance |
| 20:30:50 | `66cc898f71c7e38985adaafbb24330921b4186a5` | fix: clarify local citation repair and recover writable experiment outputs |
| 20:34:11 | `f61ea95830d2fdf9c80510320e882add3abd0d8b` | docs: record bounded A-MEM recovery phase launch |
| 20:51:50 | `a9011a58eeb16ec7695bcd307da535e669af1862` | test: preserve same-source citation diagnostic candidate and live recovery evidence |
| 21:04:46 | `1e762aad6f0293b60aa658e44c32c9972eaf4df6` | eval: preserve negative citation-format comparison and current research gap |
| 21:36:55 | `4ae8e49b6830e5eb275248c96dc265a3aa315724` | Archive isolated A-MEM holdout preparation and portable adapter checks |
| 22:10:02 | `9c76fc773fb4bfa2666c55d8697ec4fd1faaa7dd` | Archive verified host-scored cross-plan comparison candidate |
| 22:16:03 | `5b973a031ca8aaaf1431dc1044e018a020322e07` | Record integrated regression for pending V4 fixes |
| 22:33:43 | `780dd050c31b82c78eb3e80e332e43fbd9c896e4` | Archive live A-MEM index audit and exact cache cleanup receipt |
| 22:52:52 | `8b102eaf8418239389a9f7ef847f4e49ef97db9f` | fix: preserve original source identity across redacted checkpoints |
| 23:09:25 | `3b842ad3533c40eae32b9b5058f2c193e3079c04` | eval: archive complete A-MEM baseline and autonomous variant delegation |
| 23:20:27 | `65628037aac2ff221135f21b0badb67c82829e6e` | eval: prepare frozen-input candidate and ablation adapters |
| 23:46:25 | `c84e927c8739ad3e9600742a0d276aae90e4719d` | fix: preserve measured failures in bounded research feedback |
| 23:52:59 | `ea390685ff09473f24f15bb6e548e341ffbaae2c` | eval: record live research feedback replay and limits |

### 2026-10-02

| 时间 | 本地提交 SHA | 当时保存的变更摘要 |
| --- | --- | --- |
| 00:18:57 | `62c6f763797201f43d4b86a1fcffbd183abcede9` | fix: provide bounded host-derived research failure diagnostics |
| 00:50:29 | `b95633d543f7778c518b284febb9e0e11041c350` | fix: preserve experiment generation parameters and rejected-call usage |
| 01:00:17 | `0cf21b28f018b15d2d1da2fb7e2d5a2d715b4e19` | fix: forward upstream sampling in future A-MEM adapters |
| 01:19:20 | `4da01f9f6a5d9f7f60d4cbe5f53b4e4c73bc4fec` | fix: compare recorded experiment generation settings |
| 01:39:58 | `8707ba2c751c9de3befbf303fb28b8febb65053c` | docs: archive fixed-memory A-MEM development diagnostics |
| 02:14:33 | `abc8a2ebfc0f3751974b402007a777414d5c1ae7` | fix: integrate auto research recovery and evidence feedback |
| 02:18:04 | `d3f84c00dfd2090e2152139be3d01fb7269c1057` | docs: record live A-MEM continuation after integration |
| 02:43:51 | `4321c5d25ee357ee0714e761015571a3c3c0aa39` | Preserve inspect continuation windows and archive r4 baseline evidence |
| 03:10:30 | `98b8f088cf6a344a253f0b3381948394cba1cb08` | checkpoint: verify list repair candidate and audit full A-MEM result |
| 03:25:55 | `2f73a8450ef4904caafe90edb195a03e3c05db72` | eval: archive bounded list repair and remaining reading gaps |
| 03:39:40 | `3d3fa94c935dbb72c3f6b3a07754e88a1f731033` | eval: verify all baseline prompts against pinned upstream |
| 05:13:43 | `576bfb27eb02491c7c801c5302788ecc7c5b9209` | Preserve source reading progress and reserve bounded research delivery |
| 05:51:21 | `f43fd1e584350bc825699372748d03ce8fc15b83` | fix: preserve research reading continuity and audit resumable experiments |
| 06:01:29 | `cfa7a19a2a361beba49141b49355df255484c75d` | eval: prepare controlled A-MEM research with shared complete memory |
| 06:39:09 | `606a4ec4ff19b2a39a95d11c01f344a0caf700a5` | feat: locate original source definitions and preserve controlled research results |
| 06:46:26 | `4e3eafba63e3214594679ebf5176bf49770a8e39` | eval: stage disclosed MMR correction with remaining experiment budget |
| 07:15:44 | `6fa2fae658f22aa219f4ff2726e9d9ffec1b2634` | Archive corrected MMR failures and prepare frozen measurement recovery |
| 07:43:15 | `023f5371484e3346d13132e013921e8399c4a18b` | Record complete corrected MMR comparison and preregister independent holdout |
| 08:14:11 | `ea17bfd57249b74f3498507d2b99cd52b2cd0b2d` | Record independent holdout launch and isolate citation repair diagnostics |
| 08:27:57 | `b71240961fc57998611f99da500acf553653cfac` | Verify citation repair candidate with full regression and refresh project status |
| 09:07:38 | `9f4b6c9901c5b3a247c0bd30ce255b3605d6f294` | Preserve actionable plan citation diagnostics and real replay evidence |
| 09:14:55 | `b2974002ff45d77c973eadafbf220f925b7bdcf7` | Verify the combined citation diagnostics against the full offline suite |
| 09:49:04 | `9ea762af69edc3938ddbd49dc1eb331c90daa8a6` | Preserve original evidence references through research context compression |
| 10:55:32 | `785952e59f22acd8b6f717c45e785e0f1ebd6837` | eval: record current source reading and remaining gaps |
| 11:12:25 | `be41ee88b624d1d212510780c4f2c96118035bf6` | eval: preserve same-conversation source-reading continuation |
| 12:06:56 | `edca632c3288b208ee33aa08cf338df3315f5adb` | eval: audit complete first holdout conversation inputs |
| 12:15:08 | `7c2960db4f9d91ca8db17a2a964a86873e899e11` | docs: separate current auto research requirements from history |
| 14:20:32 | `bd079913e8f6247aa921f27354ba32155bd0410c` | fix: retain research citations and distinguish unstarted experiments |
| 14:35:22 | `ee7412f5b82963457e479989c6c4c7ad15c780e4` | fix: re-inspect changed research artifacts after tool updates |
| 14:38:04 | `4d6e043cfed406a507a628d146ddd72b6f0d291b` | docs: record bounded recovery authorization blocker |
| 20:09:20 | `0cd0b671031558a6f3459d6bfd1e9cb09a8673cd` | Record bounded ACL recovery and preregister same-trial holdout continuation |
| 20:21:34 | `84e0273185a6df1facea1044c2a5e84238e62909` | Document live holdout continuation and add research demo entry |
| 22:00:03 | `2fee3383d4b6aeb6730991c6f9345745ad46aa69` | Refocus current goal on complete V4 product delivery |
| 22:17:11 | `40c8ecc6323ca63a5eb9b0f887080f1e6cc03f59` | Record activation of the V4 delivery goal |
| 22:52:19 | `c3c32a97a2839e679271c9cb9d6d98b41c66aa02` | Audit V4 delivery cases and add read-only product demo |
| 23:10:39 | `8c587d44498d3057ab855d17899f3acb1ebf69fb` | Prepare terminal audit for frozen A-MEM comparison |
| 23:19:16 | `ac65cc4ea4194c2ba7b6611ef886c25212a5e7ef` | Register real ordinary-mode product acceptance |

### 2026-10-03

| 时间 | 本地提交 SHA | 当时保存的变更摘要 |
| --- | --- | --- |
| 00:05:40 | `a082a8878f03e28c47b544a5de70dbbe82fef3b7` | Complete bounded V4 delivery and repair ordinary brainstorm citations |
| 15:11:02 | `59327cb54c909d5c3153e1171421b4f4890ebcb0` | feat: finish A-MEM evaluation and add bounded parallel research |

## 公开仓库

| 提交 | 内容 |
| --- | --- |
| [63c007e](https://github.com/prossiblezero/researchagent/commit/63c007e616783d044dd1b96b4f6e020c7f27c773) | 干净源码首发，包含教程、数据集和回归输入。 |
| [226369f](https://github.com/prossiblezero/researchagent/commit/226369f341eb214a0eb0d7f1029ca924edacd700) | README 改为描述当前产品能力。 |

本目录的首次整理及后续变更直接保存在公开仓库：[查看优化历程目录的提交记录](https://github.com/prossiblezero/researchagent/commits/main/docs/optimization-history)。首次整理不在正文自写其尚未产生的 commit SHA；以 GitHub 实际提交记录为准。
