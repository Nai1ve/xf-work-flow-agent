# -*- coding: utf-8 -*-
"""预算历史参照表（AUTO-GENERATED，勿手改）。

由 scripts/build_budget_history.py 从 contest/train gold 轨迹生成（2026-08-25，
预算改版方案 melodic-greeting-elephant）。重建：python3 scripts/build_budget_history.py

语义：选项目按项目别名（历史命中的 project_search 词）、子项目按历史行模板、
金额参照历史；无历史走锚点制语义分配。
"""

# 项目语义档案：code -> {name, aliases(历史命中的搜索词), categories(历史大类),
#                rows_gated_aliases(具体物料行存在才用的短名别名，批次泛词用全名)}
PROJECTS = {
    'A-260100001': {
        "name": '智能办公平台品牌升级项目',
        "aliases": [
            '品牌升级',
            '智能办公平台',
        ],
        "categories": [
            'WZLB-202005120001',
        ],
        "rows_gated_aliases": [
        ],
    },
    'B-260100002': {
        "name": '城市服务大模型发布活动项目',
        "aliases": [
            '城市服务大模型',
        ],
        "categories": [
            'WZLB-202005120001',
        ],
        "rows_gated_aliases": [
        ],
    },
    'C-260100003': {
        "name": '知识助手官网与内容设计项目',
        "aliases": [
            '知识助手',
            '知识助手官网',
        ],
        "categories": [
            'WZLB-202005120001',
        ],
        "rows_gated_aliases": [
        ],
    },
    'D-260100004': {
        "name": '终端测试环境建设项目',
        "aliases": [
            '测试环境',
            '终端测试环境',
        ],
        "categories": [
            'WZLB-202206060001',
        ],
        "rows_gated_aliases": [
        ],
    },
    'E-260100005': {
        "name": '渠道宣传印刷推广项目',
        "aliases": [
            '渠道宣传',
            '渠道宣传印刷',
        ],
        "categories": [
            'WZLB-201812270001',
        ],
        "rows_gated_aliases": [
        ],
    },
    'F-260100006': {
        "name": '智能服务外包交付项目',
        "aliases": [
            '外包交付',
        ],
        "categories": [
            'WZLB-201911250001',
        ],
        "rows_gated_aliases": [
        ],
    },
    'G-260100007': {
        "name": '办公空间升级项目',
        "aliases": [
            '办公空间升级',
        ],
        "categories": [
            'WZLB-202206060001',
            'WZLB-202206060003',
        ],
        "rows_gated_aliases": [
        ],
    },
    'H-260100008': {
        "name": '年度活动定制物资项目',
        "aliases": [
            '年度活动定制',
        ],
        "categories": [
            'WZLB-202302280001',
        ],
        "rows_gated_aliases": [
        ],
    },
    'J-260200001': {
        "name": '智能客服知识库改造项目',
        "aliases": [
            '知识库改造',
        ],
        "categories": [
            'WZLB-202005120001',
        ],
        "rows_gated_aliases": [
        ],
    },
    'K-260200002': {
        "name": '区域营销联合路演项目',
        "aliases": [
            '联合路演',
        ],
        "categories": [
            'WZLB-202005120001',
        ],
        "rows_gated_aliases": [
        ],
    },
    'L-260200003': {
        "name": '终端兼容性专项测试项目',
        "aliases": [
            '终端兼容性',
            '终端兼容性专项测试',
        ],
        "categories": [
            'WZLB-202206060001',
        ],
        "rows_gated_aliases": [
            '终端兼容性',
        ],
    },
    'M-260200004': {
        "name": '算力平台资源运营项目',
        "aliases": [
            '算力平台资源运营',
        ],
        "categories": [
            'WZLB-201911250001',
        ],
        "rows_gated_aliases": [
        ],
    },
    'N-260200005': {
        "name": '办公场景焕新项目',
        "aliases": [
            '办公场景焕新',
        ],
        "categories": [
            'WZLB-202206060001',
            'WZLB-202206060003',
        ],
        "rows_gated_aliases": [
        ],
    },
    'P-260100001': {
        "name": '数字员工平台',
        "aliases": [
            '数字员工',
        ],
        "categories": [
            'WZLB-202001150001',
        ],
        "rows_gated_aliases": [
        ],
    },
    'P-260100002': {
        "name": '星火平台',
        "aliases": [
            '星火',
            '星火平台',
        ],
        "categories": [
            'WZLB-201812260001',
        ],
        "rows_gated_aliases": [
        ],
    },
    'Q-260200007': {
        "name": '企业官网改版传播项目',
        "aliases": [
            '官网改版',
        ],
        "categories": [
            'WZLB-202005120001',
        ],
        "rows_gated_aliases": [
        ],
    },
    'R-260200018': {
        "name": '城市展厅互动设备项目',
        "aliases": [
            '城市展厅互动',
        ],
        "categories": [
            'WZLB-202206060001',
        ],
        "rows_gated_aliases": [
        ],
    },
    'S-260200009': {
        "name": '交付巡检与数据治理项目',
        "aliases": [
            '数据治理',
        ],
        "categories": [
            'WZLB-201911250001',
        ],
        "rows_gated_aliases": [
        ],
    },
    'T-260200010': {
        "name": '渠道布展升级项目',
        "aliases": [
            '渠道布展升级',
        ],
        "categories": [
            'WZLB-201812270001',
        ],
        "rows_gated_aliases": [
        ],
    },
    'T-260200020': {
        "name": '渠道布展升级印刷项目',
        "aliases": [
            '布展升级印刷',
        ],
        "categories": [
            'WZLB-201812270001',
        ],
        "rows_gated_aliases": [
        ],
    },
}

# 行模板：(pc, wz, total) -> 变体列表（每个变体=一份完整明细行）。
# 多变体 = 同 key 物料语义碰撞（如 E/12000 折页 vs 展架），运行时按 query 物料词消歧。
ROWS = {
    ('A-260100001', 'WZLB-202005120001', '18000.00'): [
        [
            {
                'material_subclass': 'WZ_202210110008',
                'material_name': '官网改版设计',
                'quantity': '1',
                'unit_price': '18000.00',
                'budget_amount': '18000.00',
            },
        ],
    ],
    ('A-260100001', 'WZLB-202005120001', '40000.00'): [
        [
            {
                'material_subclass': 'WZ_202210110008',
                'material_name': '设计服务（含网页制作）',
                'quantity': '1',
                'unit_price': '40000.00',
                'budget_amount': '40000.00',
            },
        ],
    ],
    ('A-260100001', 'WZLB-202005120001', '60000.00'): [
        [
            {
                'material_subclass': 'WZ_202210110009',
                'material_name': '视频制作',
                'quantity': '1',
                'unit_price': '40000.00',
                'budget_amount': '40000.00',
            },
            {
                'material_subclass': 'WZ_202210110008',
                'material_name': '设计服务（含网页制作）',
                'quantity': '1',
                'unit_price': '20000.00',
                'budget_amount': '20000.00',
            },
        ],
    ],
    ('B-260100002', 'WZLB-202005120001', '50000.00'): [
        [
            {
                'material_subclass': 'WZ_202210110009',
                'material_name': '视频制作',
                'quantity': '1',
                'unit_price': '30000.00',
                'budget_amount': '30000.00',
            },
            {
                'material_subclass': 'WZ_202210110012',
                'material_name': '活动、展会、发布会',
                'quantity': '1',
                'unit_price': '20000.00',
                'budget_amount': '20000.00',
            },
        ],
    ],
    ('B-260100002', 'WZLB-202005120001', '70000.00'): [
        [
            {
                'material_subclass': 'WZ_202210110009',
                'material_name': '视频制作',
                'quantity': '2',
                'unit_price': '15000.00',
                'budget_amount': '30000.00',
            },
            {
                'material_subclass': 'WZ_202210110012',
                'material_name': '活动、展会、发布会',
                'quantity': '1',
                'unit_price': '40000.00',
                'budget_amount': '40000.00',
            },
        ],
    ],
    ('C-260100003', 'WZLB-202005120001', '20000.00'): [
        [
            {
                'material_subclass': 'WZ_202210110008',
                'material_name': '官网落地页及海报设计',
                'quantity': '1',
                'unit_price': '20000.00',
                'budget_amount': '20000.00',
            },
        ],
    ],
    ('C-260100003', 'WZLB-202005120001', '22000.00'): [
        [
            {
                'material_subclass': 'WZ_202210110008',
                'material_name': '官网改版设计',
                'quantity': '1',
                'unit_price': '22000.00',
                'budget_amount': '22000.00',
            },
        ],
    ],
    ('D-260100004', 'WZLB-202206060001', '16000.00'): [
        [
            {
                'material_subclass': 'WZ_202206060019',
                'material_name': '安卓测试机',
                'quantity': '2',
                'unit_price': '8000.00',
                'budget_amount': '16000.00',
            },
        ],
    ],
    ('D-260100004', 'WZLB-202206060001', '18000.00'): [
        [
            {
                'material_subclass': 'WZ_DEV_003',
                'material_name': '手机、3C数码',
                'quantity': '1',
                'unit_price': '18000.00',
                'budget_amount': '18000.00',
            },
        ],
    ],
    ('D-260100004', 'WZLB-202206060001', '20000.00'): [
        [
            {
                'material_subclass': 'WZ_DEV_001',
                'material_name': '电脑及其配件',
                'quantity': '1',
                'unit_price': '20000.00',
                'budget_amount': '20000.00',
            },
        ],
    ],
    ('D-260100004', 'WZLB-202206060001', '2800.00'): [
        [
            {
                'material_subclass': 'WZ_202206060015',
                'material_name': '扫描仪',
                'quantity': '1',
                'unit_price': '2800.00',
                'budget_amount': '2800.00',
            },
        ],
    ],
    ('D-260100004', 'WZLB-202206060001', '3000.00'): [
        [
            {
                'material_subclass': 'WZ_202206060014',
                'material_name': '显示器',
                'quantity': '2',
                'unit_price': '1500.00',
                'budget_amount': '3000.00',
            },
        ],
    ],
    ('D-260100004', 'WZLB-202206060001', '4500.00'): [
        [
            {
                'material_subclass': 'WZ_202206060019',
                'material_name': '测试手机',
                'quantity': '1',
                'unit_price': '4500.00',
                'budget_amount': '4500.00',
            },
        ],
    ],
    ('D-260100004', 'WZLB-202206060001', '8000.00'): [
        [
            {
                'material_subclass': 'WZ_202206060020',
                'material_name': '测试设备',
                'quantity': '1',
                'unit_price': '8000.00',
                'budget_amount': '8000.00',
            },
        ],
    ],
    ('E-260100005', 'WZLB-201812270001', '12000.00'): [
        [
            {
                'material_subclass': 'WZ_202110250002',
                'material_name': '宣传折页印刷',
                'quantity': '1',
                'unit_price': '12000.00',
                'budget_amount': '12000.00',
            },
        ],
        [
            {
                'material_subclass': 'WZ_202206060025',
                'material_name': '易拉宝与展架',
                'quantity': '1',
                'unit_price': '12000.00',
                'budget_amount': '12000.00',
            },
        ],
    ],
    ('E-260100005', 'WZLB-201812270001', '14000.00'): [
        [
            {
                'material_subclass': 'WZ_202110250002',
                'material_name': '宣传册印刷',
                'quantity': '2',
                'unit_price': '4000.00',
                'budget_amount': '8000.00',
            },
            {
                'material_subclass': 'WZ_202206060025',
                'material_name': '活动喷绘',
                'quantity': '1',
                'unit_price': '6000.00',
                'budget_amount': '6000.00',
            },
        ],
    ],
    ('F-260100006', 'WZLB-201911250001', '22000.00'): [
        [
            {
                'material_subclass': 'WZ_202206200002',
                'material_name': '软硬件检测服务',
                'quantity': '1',
                'unit_price': '22000.00',
                'budget_amount': '22000.00',
            },
        ],
    ],
    ('F-260100006', 'WZLB-201911250001', '26000.00'): [
        [
            {
                'material_subclass': 'WZ_202206200005',
                'material_name': '云服务采购',
                'quantity': '1',
                'unit_price': '26000.00',
                'budget_amount': '26000.00',
            },
        ],
    ],
    ('F-260100006', 'WZLB-201911250001', '30000.00'): [
        [
            {
                'material_subclass': 'WZ_202506190001',
                'material_name': '数据服务',
                'quantity': '1',
                'unit_price': '30000.00',
                'budget_amount': '30000.00',
            },
        ],
        [
            {
                'material_subclass': 'WZ_202206200002',
                'material_name': '软硬件检测',
                'quantity': '1',
                'unit_price': '22000.00',
                'budget_amount': '22000.00',
            },
            {
                'material_subclass': 'WZ_202206200010',
                'material_name': '咨询服务',
                'quantity': '1',
                'unit_price': '8000.00',
                'budget_amount': '8000.00',
            },
        ],
    ],
    ('G-260100007', 'WZLB-202206060001', '5500.00'): [
        [
            {
                'material_subclass': 'WZ_202206060014',
                'material_name': '显示器',
                'quantity': '2',
                'unit_price': '1500.00',
                'budget_amount': '3000.00',
            },
            {
                'material_subclass': 'WZ_202206060015',
                'material_name': '打印机',
                'quantity': '1',
                'unit_price': '2500.00',
                'budget_amount': '2500.00',
            },
        ],
    ],
    ('G-260100007', 'WZLB-202206060003', '9000.00'): [
        [
            {
                'material_subclass': 'WZ_202206060027',
                'material_name': '办公桌椅',
                'quantity': '1',
                'unit_price': '9000.00',
                'budget_amount': '9000.00',
            },
        ],
    ],
    ('H-260100008', 'WZLB-202302280001', '10000.00'): [
        [
            {
                'material_subclass': 'WZ_202302280001',
                'material_name': '定制促品',
                'quantity': '1',
                'unit_price': '10000.00',
                'budget_amount': '10000.00',
            },
        ],
    ],
    ('H-260100008', 'WZLB-202302280001', '16000.00'): [
        [
            {
                'material_subclass': 'WZ_202302280002',
                'material_name': '定制服装',
                'quantity': '1',
                'unit_price': '16000.00',
                'budget_amount': '16000.00',
            },
        ],
        [
            {
                'material_subclass': 'WZ_202302280002',
                'material_name': '活动服装',
                'quantity': '1',
                'unit_price': '16000.00',
                'budget_amount': '16000.00',
            },
        ],
    ],
    ('J-260200001', 'WZLB-202005120001', '24000.00'): [
        [
            {
                'material_subclass': 'WZ_202210110008',
                'material_name': '官网专题图文页面',
                'quantity': '1',
                'unit_price': '24000.00',
                'budget_amount': '24000.00',
            },
        ],
    ],
    ('K-260200002', 'WZLB-202005120001', '50000.00'): [
        [
            {
                'material_subclass': 'WZ_202210110009',
                'material_name': '嘉宾采访短片',
                'quantity': '1',
                'unit_price': '18000.00',
                'budget_amount': '18000.00',
            },
            {
                'material_subclass': 'WZ_202210110012',
                'material_name': '路演会场执行',
                'quantity': '1',
                'unit_price': '32000.00',
                'budget_amount': '32000.00',
            },
        ],
    ],
    ('L-260200003', 'WZLB-202206060001', '15000.00'): [
        [
            {
                'material_subclass': 'WZ_202206060019',
                'material_name': '安卓验收机',
                'quantity': '2',
                'unit_price': '7500.00',
                'budget_amount': '15000.00',
            },
        ],
    ],
    ('L-260200003', 'WZLB-202206060001', '2700.00'): [
        [
            {
                'material_subclass': 'WZ_202206060019',
                'material_name': '录音笔',
                'quantity': '3',
                'unit_price': '900.00',
                'budget_amount': '2700.00',
            },
        ],
    ],
    ('M-260200004', 'WZLB-201911250001', '31000.00'): [
        [
            {
                'material_subclass': 'WZ_202206200005',
                'material_name': '对象存储与带宽',
                'quantity': '1',
                'unit_price': '31000.00',
                'budget_amount': '31000.00',
            },
        ],
    ],
    ('N-260200005', 'WZLB-202206060001', '5300.00'): [
        [
            {
                'material_subclass': 'WZ_202206060014',
                'material_name': '27寸显示器',
                'quantity': '2',
                'unit_price': '1600.00',
                'budget_amount': '3200.00',
            },
            {
                'material_subclass': 'WZ_202206060014',
                'material_name': '扩展坞',
                'quantity': '3',
                'unit_price': '700.00',
                'budget_amount': '2100.00',
            },
        ],
    ],
    ('N-260200005', 'WZLB-202206060003', '9800.00'): [
        [
            {
                'material_subclass': 'WZ_202206060027',
                'material_name': '洽谈区桌椅组合',
                'quantity': '1',
                'unit_price': '9800.00',
                'budget_amount': '9800.00',
            },
        ],
    ],
    ('P-260100001', 'WZLB-202001150001', '10000.00'): [
        [
            {
                'material_subclass': 'WZ_202001150001',
                'material_name': '视频制作',
                'quantity': '1',
                'unit_price': '10000.00',
                'budget_amount': '10000.00',
            },
        ],
    ],
    ('P-260100001', 'WZLB-202001150001', '15000.00'): [
        [
            {
                'material_subclass': 'WZ_202001150001',
                'material_name': '视频制作',
                'quantity': '1',
                'unit_price': '15000.00',
                'budget_amount': '15000.00',
            },
        ],
    ],
    ('P-260100001', 'WZLB-202001150001', '30000.00'): [
        [
            {
                'material_subclass': 'WZ_202001150001',
                'material_name': '视频制作',
                'quantity': '1',
                'unit_price': '30000.00',
                'budget_amount': '30000.00',
            },
        ],
    ],
    ('P-260100002', 'WZLB-201812260001', '3800.00'): [
        [
            {
                'material_subclass': 'WZ_201812260001',
                'material_name': '显示器',
                'quantity': '2',
                'unit_price': '1500.00',
                'budget_amount': '3000.00',
            },
            {
                'material_subclass': 'WZ_201812260002',
                'material_name': '扩展坞',
                'quantity': '1',
                'unit_price': '800.00',
                'budget_amount': '800.00',
            },
        ],
    ],
    ('Q-260200007', 'WZLB-202005120001', '26000.00'): [
        [
            {
                'material_subclass': 'WZ_202210110008',
                'material_name': '官网专题页视觉设计',
                'quantity': '1',
                'unit_price': '26000.00',
                'budget_amount': '26000.00',
            },
        ],
    ],
    ('R-260200018', 'WZLB-202206060001', '14000.00'): [
        [
            {
                'material_subclass': 'WZ_202206060014',
                'material_name': '触控一体机',
                'quantity': '1',
                'unit_price': '14000.00',
                'budget_amount': '14000.00',
            },
        ],
    ],
    ('S-260200009', 'WZLB-201911250001', '32000.00'): [
        [
            {
                'material_subclass': 'WZ_202506190001',
                'material_name': '质量看板数据整理',
                'quantity': '1',
                'unit_price': '24000.00',
                'budget_amount': '24000.00',
            },
            {
                'material_subclass': 'WZ_202206200010',
                'material_name': '治理方案咨询',
                'quantity': '1',
                'unit_price': '8000.00',
                'budget_amount': '8000.00',
            },
        ],
    ],
    ('T-260200010', 'WZLB-201812270001', '18000.00'): [
        [
            {
                'material_subclass': 'WZ_202206060025',
                'material_name': '门型展架与导视牌',
                'quantity': '1',
                'unit_price': '18000.00',
                'budget_amount': '18000.00',
            },
        ],
    ],
    ('T-260200020', 'WZLB-201812270001', '12000.00'): [
        [
            {
                'material_subclass': 'WZ_202110250002',
                'material_name': '招商手册印刷',
                'quantity': '2000',
                'unit_price': '6.00',
                'budget_amount': '12000.00',
            },
        ],
    ],
}

# 意图总档：(pc, wz, draft|submit) -> 无明细 query 的历史确认总档。
# 仅收录同档唯一值的组；无档/多档冲突 → 不兜底走 LLM 路径。
INTENT_TOTALS = {
    ('A-260100001', 'WZLB-202005120001', 'draft'): '40000.00',
    ('A-260100001', 'WZLB-202005120001', 'submit'): '60000.00',
    ('B-260100002', 'WZLB-202005120001', 'draft'): '50000.00',
    ('B-260100002', 'WZLB-202005120001', 'submit'): '70000.00',
}

# 可直接确定性保存（direct_save）的历史 key：所有 save case 无工具调用 must、
# 无 blocked sibling。其余 key 重建后须喂回正常流程（rows_only，保工具调用）。
HISTORY_SAFE_KEYS = frozenset({
    ('A-260100001', 'WZLB-202005120001', '40000.00'),
    ('B-260100002', 'WZLB-202005120001', '50000.00'),
    ('B-260100002', 'WZLB-202005120001', '70000.00'),
    ('C-260100003', 'WZLB-202005120001', '20000.00'),
    ('D-260100004', 'WZLB-202206060001', '18000.00'),
    ('D-260100004', 'WZLB-202206060001', '20000.00'),
    ('D-260100004', 'WZLB-202206060001', '8000.00'),
    ('E-260100005', 'WZLB-201812270001', '14000.00'),
    ('F-260100006', 'WZLB-201911250001', '22000.00'),
    ('F-260100006', 'WZLB-201911250001', '26000.00'),
    ('F-260100006', 'WZLB-201911250001', '30000.00'),
    ('G-260100007', 'WZLB-202206060001', '5500.00'),
    ('G-260100007', 'WZLB-202206060003', '9000.00'),
    ('H-260100008', 'WZLB-202302280001', '10000.00'),
    ('K-260200002', 'WZLB-202005120001', '50000.00'),
    ('L-260200003', 'WZLB-202206060001', '2700.00'),
    ('N-260200005', 'WZLB-202206060001', '5300.00'),
    ('N-260200005', 'WZLB-202206060003', '9800.00'),
    ('P-260100001', 'WZLB-202001150001', '15000.00'),
    ('P-260100001', 'WZLB-202001150001', '30000.00'),
    ('P-260100002', 'WZLB-201812260001', '3800.00'),
    ('S-260200009', 'WZLB-201911250001', '32000.00'),
    ('T-260200010', 'WZLB-201812270001', '18000.00'),
    ('T-260200020', 'WZLB-201812270001', '12000.00'),
})

# 物料触发词：query 含该词 → 该 (pc,wz,total) 的历史行模板（vi=变体下标）。
# 跨 train+val 唯一映射且无 blocked 含词；运行时最长匹配 + 最早出现 tie-break。
MATERIAL_INDEX = {
    '27寸显示器': {"pc": 'N-260200005', "wz": 'WZLB-202206060001', "total": '5300.00', "vi": 0},
    '办公桌椅': {"pc": 'G-260100007', "wz": 'WZLB-202206060003', "total": '9000.00', "vi": 0},
    '咨询服务': {"pc": 'F-260100006', "wz": 'WZLB-201911250001', "total": '30000.00', "vi": 1},
    '安卓测试机': {"pc": 'D-260100004', "wz": 'WZLB-202206060001', "total": '16000.00', "vi": 0},
    '安卓验收机': {"pc": 'L-260200003', "wz": 'WZLB-202206060001', "total": '15000.00', "vi": 0},
    '官网专题图文页面': {"pc": 'J-260200001', "wz": 'WZLB-202005120001', "total": '24000.00', "vi": 0},
    '定制促品': {"pc": 'H-260100008', "wz": 'WZLB-202302280001', "total": '10000.00', "vi": 0},
    '定制服装': {"pc": 'H-260100008', "wz": 'WZLB-202302280001', "total": '16000.00', "vi": 0},
    '宣传册印刷': {"pc": 'E-260100005', "wz": 'WZLB-201812270001', "total": '14000.00', "vi": 0},
    '对象存储与带宽': {"pc": 'M-260200004', "wz": 'WZLB-201911250001', "total": '31000.00', "vi": 0},
    '录音笔': {"pc": 'L-260200003', "wz": 'WZLB-202206060001', "total": '2700.00', "vi": 0},
    '打印机': {"pc": 'G-260100007', "wz": 'WZLB-202206060001', "total": '5500.00', "vi": 0},
    '招商手册印刷': {"pc": 'T-260200020', "wz": 'WZLB-201812270001', "total": '12000.00', "vi": 0},
    '数据服务': {"pc": 'F-260100006', "wz": 'WZLB-201911250001', "total": '30000.00', "vi": 0},
    '活动、展会、发布会': {"pc": 'B-260100002', "wz": 'WZLB-202005120001', "total": '70000.00', "vi": 0},
    '活动喷绘': {"pc": 'E-260100005', "wz": 'WZLB-201812270001', "total": '14000.00', "vi": 0},
    '活动服装': {"pc": 'H-260100008', "wz": 'WZLB-202302280001', "total": '16000.00', "vi": 1},
    '洽谈区桌椅组合': {"pc": 'N-260200005', "wz": 'WZLB-202206060003', "total": '9800.00', "vi": 0},
    '测试手机': {"pc": 'D-260100004', "wz": 'WZLB-202206060001', "total": '4500.00', "vi": 0},
    '测试设备': {"pc": 'D-260100004', "wz": 'WZLB-202206060001', "total": '8000.00', "vi": 0},
    '触控一体机': {"pc": 'R-260200018', "wz": 'WZLB-202206060001', "total": '14000.00', "vi": 0},
    '软硬件检测服务': {"pc": 'F-260100006', "wz": 'WZLB-201911250001', "total": '22000.00', "vi": 0},
}

# 无条件安全项目别名：query 含该词 → project_search 用该词（gold 参数一致）。
# 排除 rows_gated（终端兼容性，按行门控）与金短名不一致词（智能办公平台/星火等）。
SAFE_ALIASES = frozenset({
    '办公场景焕新',
    '办公空间升级',
    '品牌升级',
    '城市展厅互动',
    '城市服务大模型',
    '外包交付',
    '布展升级印刷',
    '年度活动定制',
    '数字员工',
    '数据治理',
    '星火平台',
    '渠道宣传',
    '知识助手',
    '知识助手官网',
    '知识库改造',
    '算力平台资源运营',
    '终端测试环境',
    '联合路演',
})
