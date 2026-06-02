import logging
import sqlite3
import json
from datetime import datetime, timedelta
import pytz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler, ChatMemberHandler
import re
import threading
from flask import Flask, request, jsonify
import os
import asyncio
import random
from telegram.error import RetryAfter, TelegramError

# ==================== 系统基础配置 ====================
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# ⚠️ 重要：请务必在此处替换为您去 @BotFather 重新获取的全新 Token
TOKEN = "8617895746:AAEEJtvChCL0t_G4jvRE9D7FSVo_54-LnWQ"
WEB_URL = "https://shishi-669dk.onrender.com"
PORT = int(os.environ.get('PORT', 8080))

FOUNDER_USERS = [8179896441]
TRON_ADDRESS = "TVnjLwDrGjYVRTa1ukfoE2mFTmCxtrjoCw"

PRICE_1_MONTH = 80
PRICE_2_MONTH = 130
PRICE_3_MONTH = 220

flask_app = Flask(__name__)

# ==================== 🛡️ 工业级异步高并发防封发送引擎 🛡️ ====================
class TelegramSmartLimiter:
    def __init__(self):
        # 严守官方全网每秒最多 30 条死线，设定全局并发信号量为 25
        self.global_semaphore = asyncio.Semaphore(25)
        self.group_locks = {}

    def get_group_lock(self, group_id):
        if group_id not in self.group_locks:
            self.group_locks[group_id] = asyncio.Lock()
        return self.group_locks[group_id]

limiter = TelegramSmartLimiter()

async def safe_send_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str, parse_mode="HTML", reply_markup=None):
    group_lock = limiter.get_group_lock(chat_id)
    
    # 核心流量整形：全局限流与单群平滑发送双重锁
    async with limiter.global_semaphore:
        async with group_lock:
            for attempt in range(3):
                try:
                    await context.bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode, reply_markup=reply_markup)
                    # 满足大账单高频吞吐流：发出后进行 0.25 秒微秒级平滑交替，彻底打散发送指纹，防止官方风控检测
                    await asyncio.sleep(0.25)
                    return True
                except RetryAfter as e:
                    logging.warning(f"⚠️ 触发 Telegram 瞬时洪水防御！要求强制等待 {e.retry_after} 秒")
                    await asyncio.sleep(e.retry_after + 1)
                except TelegramError as e:
                    logging.error(f"❌ 目标群组/用户 {chat_id} 发送失败，机器人可能已被踢出群组: {e}")
                    return False
    return False

# ==================== 数据库引擎 ====================
def get_db_connection():
    conn = sqlite3.connect('bot_data.db', timeout=60.0)
    conn.execute('PRAGMA journal_mode=WAL;')
    conn.execute('PRAGMA synchronous=NORMAL;')
    return conn

def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS settings
                 (group_id INTEGER PRIMARY KEY, operators TEXT DEFAULT '[]', exchange_rate REAL DEFAULT 7.2,
                  fee_rate REAL DEFAULT 0, is_active INTEGER DEFAULT 0, language TEXT DEFAULT 'chinese',
                  timezone TEXT DEFAULT 'Asia/Shanghai', show_usdt INTEGER DEFAULT 1, expire_time TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS bills
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER, user_id INTEGER, username TEXT,
                  remark TEXT, amount REAL, usdt_amount REAL, exchange_rate REAL, bill_type TEXT,
                  timestamp TEXT, date_str TEXT, is_settled INTEGER DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS vip_users
                 (user_id INTEGER PRIMARY KEY, username TEXT, expire_time TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS dynamic_masters
                 (user_id INTEGER PRIMARY KEY, username TEXT, added_by INTEGER)''')
    c.execute('''CREATE TABLE IF NOT EXISTS user_caches
                 (username_lower TEXT PRIMARY KEY, user_id INTEGER, display_name TEXT)''')
    conn.commit()
    conn.close()

def save_user_cache(user_id, username, first_name):
    if not username: return
    username_lower = username.lower()
    display_name = f"@{username}"
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO user_caches (username_lower, user_id, display_name) VALUES (?, ?, ?)",
                  (username_lower, user_id, display_name))
        conn.commit()
        conn.close()
    except: pass

def get_user_id_by_username(username_str):
    if not username_str: return None, None
    username_lower = username_str.replace('@', '').strip().lower()
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT user_id, display_name FROM user_caches WHERE username_lower = ?", (username_lower,))
        row = c.fetchone()
        conn.close()
        if row: return row[0], row[1]
    except: pass
    return None, None

def get_current_time(timezone_str):
    try:
        tz = pytz.timezone(timezone_str)
        now = datetime.now(tz)
        return now, now.strftime("%H:%M:%S"), now.strftime("%Y-%m-%d %H:%M:%S")
    except:
        tz = pytz.timezone('Asia/Shanghai')
        now = datetime.now(tz)
        return now, now.strftime("%H:%M:%S"), now.strftime("%Y-%m-%d %H:%M:%S")

# ==================== 权限与有效判定核心 ====================
def get_all_masters():
    masters = list(FOUNDER_USERS)
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT user_id FROM dynamic_masters")
        rows = c.fetchall()
        conn.close()
        for row in rows:
            if row[0] not in masters: masters.append(row[0])
    except: pass
    return masters

def is_master(user_id):
    return user_id in get_all_masters()

def get_dynamic_masters_by_creator(creator_id):
    try:
        conn = get_db_connection()
        c = conn.cursor()
        if creator_id in FOUNDER_USERS:
            c.execute("SELECT user_id, username FROM dynamic_masters")
        else:
            c.execute("SELECT user_id, username FROM dynamic_masters WHERE added_by = ?", (creator_id,))
        rows = c.fetchall()
        conn.close()
        return rows
    except: return []

def is_vip_user(user_id):
    if user_id in FOUNDER_USERS: return True
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT expire_time FROM vip_users WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        conn.close()
        if row:
            expire = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
            return datetime.now() < expire
    except: pass
    return False

def check_group_validity(group_id, user_id=None):
    if user_id and is_master(user_id):
        return True, "MASTER_BYPASS"
    if user_id and is_vip_user(user_id):
        return True, "VIP_DIRECT_PASS"

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT operators FROM settings WHERE group_id = ?", (group_id,))
    row = c.fetchone()
    
    if row:
        try:
            ops = json.loads(row[0] or '[]')
            for op_id in ops:
                if is_vip_user(op_id):
                    conn.close()
                    return True, "VIP_OPERATOR_VALID"
        except: pass
        conn.close()
    else:
        tz_str = 'Asia/Shanghai'
        _, _, init_time = get_current_time(tz_str)
        c.execute("INSERT OR IGNORE INTO settings (group_id, operators, exchange_rate, fee_rate, is_active, language, timezone, show_usdt, expire_time) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                  (group_id, '[]', 7.2, 0, 0, 'chinese', 'Asia/Shanghai', 1, init_time))
        conn.commit()
        conn.close()

    return False, "EXPIRED"

def can_use(group_id, user_id):
    if is_master(user_id) or is_vip_user(user_id): return True
    try:
        ops = json.loads(get_setting(group_id, 'operators') or '[]')
        return user_id in ops
    except: return False

def get_setting(group_id, key):
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT * FROM settings WHERE group_id = ?", (group_id,))
        row = c.fetchone()
        conn.close()
        if not row: return None
        cols = ['group_id', 'operators', 'exchange_rate', 'fee_rate', 'is_active', 'language', 'timezone', 'show_usdt', 'expire_time']
        return dict(zip(cols, row)).get(key)
    except: return None

def update_setting(group_id, key, value):
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT * FROM settings WHERE group_id = ?", (group_id,))
        if c.fetchone():
            c.execute(f"UPDATE settings SET {key} = ? WHERE group_id = ?", (value, group_id))
        else:
            trial_expire = (datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
            c.execute("INSERT INTO settings (group_id, operators, exchange_rate, fee_rate, is_active, language, timezone, show_usdt, expire_time) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      (group_id, '[]', 7.2, 0, 0, 'chinese', 'Asia/Shanghai', 1, trial_expire))
            c.execute(f"UPDATE settings SET {key} = ? WHERE group_id = ?", (value, group_id))
        conn.commit()
        conn.close()
    except: pass

# ==================== 账目数据内核 ====================
def add_bill(group_id, user_id, username, remark, amount, bill_type, exchange_rate=None):
    if exchange_rate is None:
        exchange_rate = get_setting(group_id, 'exchange_rate') or 7.2
    
    if bill_type == 'income':
        usdt_amount = amount / exchange_rate
    else:
        usdt_amount = amount

    tz_str = get_setting(group_id, 'timezone') or 'Asia/Shanghai'
    now, _, full_time = get_current_time(tz_str)
    date_str = now.strftime("%Y-%m-%d")

    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''INSERT INTO bills 
                 (group_id, user_id, username, remark, amount, usdt_amount, exchange_rate, bill_type, timestamp, date_str, is_settled)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)''',
              (group_id, user_id, username, remark, amount, usdt_amount, exchange_rate, bill_type, full_time, date_str))
    conn.commit()
    conn.close()
    return usdt_amount

def get_class_bills_by_date(group_id, target_date):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT remark, username, amount, usdt_amount, exchange_rate, timestamp FROM bills WHERE group_id = ? AND date_str = ? AND bill_type = 'income' ORDER BY id ASC", (group_id, target_date))
    income = c.fetchall()
    c.execute("SELECT remark, username, usdt_amount, exchange_rate, timestamp FROM bills WHERE group_id = ? AND date_str = ? AND bill_type = 'expense' ORDER BY id ASC", (group_id, target_date))
    expense = c.fetchall()
    c.execute("SELECT SUM(amount), SUM(usdt_amount) FROM bills WHERE group_id = ? AND date_str = ? AND bill_type = 'income'", (group_id, target_date))
    total_income = c.fetchone()
    c.execute("SELECT SUM(usdt_amount) FROM bills WHERE group_id = ? AND date_str = ? AND bill_type = 'expense'", (group_id, target_date))
    total_expense = c.fetchone()
    conn.close()
    return income, expense, total_income, total_expense

# ==================== 统一账目文本渲染引擎 ====================
async def send_text_bill_report(update, gid, target_date, context: ContextTypes.DEFAULT_TYPE):
    rate = get_setting(gid, 'exchange_rate') or 7.2
    income, expense, total_income, total_expense = get_class_bills_by_date(gid, target_date)

    total_rmb = total_income[0] if (total_income and total_income[0]) else 0
    total_usdt = total_income[1] if (total_income and total_income[1]) else 0
    expense_usdt = total_expense[0] if (total_expense and total_expense[0]) else 0
    remaining_usdt = total_usdt - expense_usdt

    random_fingerprint = f"\n\n<code>[核算编号: {random.randint(1000,9999)}]</code>"

    # 完全重现最初始、最完整的长账单大报告样式
    report = f"📊 <b>账单汇总 ({target_date})</b>\n\n"
    
    report += "📥 <b>入款 (仅显示最后5笔):</b>\n"
    if income:
        for row in income[-5:]:
            remark, username, amount, usdt_amount, ex_rate, timestamp = row
            time_str = timestamp[11:16] if timestamp else "00:00"
            rem_part = f" ({remark})" if remark else ""
            report += f"  {time_str} {amount:.0f}/{ex_rate:.2f}= {usdt_amount:.1f}U{rem_part}\n"
    else:
        report += "  暂无任何入款数据\n"

    if expense:
        report += "\n📤 <b>下发 (仅显示最后5笔):</b>\n"
        for row in expense[-5:]:
            remark, username, usdt_amount, ex_rate, timestamp = row
            time_str = timestamp[11:16] if timestamp else "00:00"
            rem_part = f" ({remark})" if remark else ""
            report += f"  {time_str} 下发 {usdt_amount:.1f}U{rem_part}\n"

    report += f"\n💰 <b>汇率:</b> {rate:.2f}\n"
    report += f"📊 <b>总入款:</b> {total_rmb:.0f} | {total_usdt:.1f}U\n"
    report += f"📊 <b>已下发:</b> {expense_usdt:.1f}U\n"
    report += f"📊 <b>未下发:</b> {remaining_usdt:.1f}U"
    report += random_fingerprint

    # 包含网页端实时看账看板的按钮链接
    keyboard = [[InlineKeyboardButton("📊 查看完整账单 (Web)", url=f"{WEB_URL}?group_id={gid}")]]
    await safe_send_message(context, chat_id=gid, text=report, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

# ==================== 中缅双语帮助文本引擎 ====================
def generate_help_text(lang='chinese'):
    if lang == 'myanmar':
        return """🤖 *စာရင်းကိုင်ဘော့ အကူအညီ* (Help)
📌 *စာရင်းသွင်းရန် ပုံစံများ：*
`+1000` - Ngwe Win 1000 Kyat
`-1000` - Ngwe Win -1000 Kyat
`Thut50` / `下发50` - 50 USDT Thut Ranyan
`+0` - YaNay SaYinChoke KyiRanyan"""
    else:
        return """🤖 *记账机器人使用指南*
📌 *记账格式：*
`+1000` - 入款1000元
`-1000` - 入款-1000元 (扣减款)
`备注+2000` - 带备注入款
`下发50` / `ထုတ်50` - 下发50 USDT
`+0` - 查看今日汇总

📌 *删除命令：*
`删今天` - 清空今日所有账单
`全部清单` - 清空本群历史全部记录
`清单+备注` - 删除今天指定备注的进单记录"""

# ==================== 网页端对账看板网关 ====================
@flask_app.route('/')
def index():
    return '''<!DOCTYPE html><html><head><meta charset="UTF-8"><title>实时课堂账单历史明细</title></head><body><h2 style="text-align:center;margin-top:50px;">实时多群分布式网页对账看板正在就绪中...</h2></body></html>'''

@flask_app.route('/api/bill')
def api_bill():
    try:
        group_id_str = request.args.get('group_id', default='0').strip()
        try: group_id = int(group_id_str)
        except: group_id = 0
        tz_str = get_setting(group_id, 'timezone') or 'Asia/Shanghai'
        now, _, _ = get_current_time(tz_str)
        target_date = request.args.get('date', default=now.strftime("%Y-%m-%d"))
        income, expense, total_income, total_expense = get_class_bills_by_date(group_id, target_date)
        rate = get_setting(group_id, 'exchange_rate') or 7.2
        fee_rate = get_setting(group_id, 'fee_rate') or 0
        total_rmb = total_income[0] if (total_income and total_income[0]) else 0
        total_usdt = total_income[1] if (total_income and total_income[1]) else 0
        expense_usdt = total_expense[0] if (total_expense and total_expense[0]) else 0
        income_bills = [{'remark': r[0] or '-', 'username': r[1] or '未知', 'amount': f"{r[2]:.0f}", 'usdt': f"{r[3]:.2f}", 'exchange_rate': f"{r[4]:.2f}", 'time': r[5][11:19] if r[5] else ''} for r in income]
        expense_bills = [{'remark': r[0] or '-', 'username': r[1] or '未知', 'usdt': f"{r[2]:.2f}", 'time': r[4][11:19] if r[4] else ''} for r in expense]
        return jsonify({'exchange_rate': f"{rate:.2f}", 'fee_rate': f"{fee_rate:.0f}", 'total_rmb': f"{total_rmb:.0f}", 'total_usdt': f"{total_usdt:.2f}", 'expense_usdt': f"{expense_usdt:.2f}", 'remaining_usdt': f"{total_usdt - expense_usdt:.2f}", 'income_bills': income_bills, 'expense_bills': expense_bills, 'summary_by_remark': {}})
    except Exception as e: return jsonify({'error': True, 'msg': str(e)}), 500

def get_private_reply_keyboard():
    keyboard = [
        [KeyboardButton("到期时间"), KeyboardButton("详细说明书")],
        [KeyboardButton("自助续费"), KeyboardButton("如何设置权限人")],
        [KeyboardButton("取掉权限人"), KeyboardButton("开启/关闭计算功能")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, input_field_placeholder="请选择业务管理面板")

# ==================== 🛠️ 核心新增：自动加入新群打招呼欢迎处理器 ====================
async def on_bot_join_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 判定机器人自身状态改变
    if not update.my_chat_member:
        return
        
    old_status = update.my_chat_member.old_chat_member.status
    new_status = update.my_chat_member.new_chat_member.status
    gid = update.my_chat_member.chat.id
    chat_type = update.my_chat_member.chat.type

    # 当机器人被新“加入”到超级群组或普通群组时触发
    if chat_type in ["group", "supergroup"] and new_status in ["member", "administrator"] and old_status not in ["member", "administrator"]:
        # 自动初始化这个新群的数据库默认基础配置
        check_group_validity(gid)
        
        # 自动发送进群激活指引，完美符合您的指定文本要求
        welcome_text = (
            "感谢您把我添加到贵群!\n"
            "下一步为我可以开始记账，请发：<code>上课</code>"
        )
        await safe_send_message(context, chat_id=gid, text=welcome_text, parse_mode="HTML")

# ==================== 商业化业务层处理器 ====================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    gid = update.effective_chat.id
    if update.effective_chat.type == "private":
        save_user_cache(uid, update.effective_user.username, update.effective_user.first_name)
        welcome = (
            "<b>欢迎使用多群分布式商用记账系统</b>\n\n"
            "将本机器人直接加入您所需要管理的多个业务群组即可使用。\n"
            "群组使用权限将自动联动您的商用买家身份账户续费周期。"
        )
        await safe_send_message(context, uid, welcome, parse_mode="HTML", reply_markup=get_private_reply_keyboard())
    else:
        await safe_send_message(context, gid, "📊 多群分布式智能记账核算核心已部署就绪！输入 <code>上课</code> 启动录入。")

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = query.from_user.id
    await query.answer()

    if query.data.startswith("v_approve_"):
        parts = query.data.split("_")
        t_uid = int(parts[2])
        m_count = int(parts[3])
        days = m_count * 30
        
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT expire_time FROM vip_users WHERE user_id = ?", (t_uid,))
        row = c.fetchone()
        if row:
            try:
                curr = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
                base = curr if curr > datetime.now() else datetime.now()
            except: base = datetime.now()
        else: base = datetime.now()
        
        new_expire = base + timedelta(days=days)
        exp_str = new_expire.strftime("%Y-%m-%d %H:%M:%S")
        c.execute("INSERT OR REPLACE INTO vip_users (user_id, username, expire_time) VALUES (?, '商用买家', ?)", (t_uid, exp_str))
        conn.commit()
        conn.close()
        
        await query.message.edit_caption(f"✅ 审核成功！买家资格已延期至：\n<code>{exp_str}</code>", parse_mode="HTML")
        try: await safe_send_message(context, t_uid, f"🎉 恭喜！您的自助充值申请已审核通过！\n您名下的所有授权群聊均已自动同步延期！\n到期时间更新为：{exp_str}")
        except: pass
    
    elif query.data.startswith("v_reject_"):
        t_uid = int(query.data.split("_")[2])
        await query.message.edit_caption("❌ 账目不符，已驳回此转账截图。")
        try: await safe_send_message(context, t_uid, "⚠️ 您的自助续费凭证未通过审核，请检查真实账目后再次提交。")
        except: pass

# ==================== 文字指令网关核心处理 ====================
async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    chat_type = update.effective_chat.type
    gid = update.effective_chat.id
    uid = update.effective_user.id
    username = update.effective_user.first_name or "记账员"

    if update.effective_user:
        save_user_cache(uid, update.effective_user.username, update.effective_user.first_name)

    if chat_type == "private":
        if text == "到期时间":
            if uid in FOUNDER_USERS:
                await safe_send_message(context, uid, "👑 ⚖️ <b>创始人至尊永久账户（免续费）</b>", parse_mode="HTML")
                return
            conn = get_db_connection()
            c = conn.cursor()
            c.execute("SELECT expire_time FROM vip_users WHERE user_id = ?", (uid,))
            row = c.fetchone()
            conn.close()
            if row: await safe_send_message(context, uid, f"📅 您的商用买家VIP多群授权截止时间为：\n<code>{row[0]}</code>", parse_mode="HTML")
            else: await safe_send_message(context, uid, "⚠️ <b>您目前无任何有效商用授权。请选择 [自助续费] 订购。</b>", parse_mode="HTML")
        elif text == "详细说明书":
            await safe_send_message(context, uid, generate_help_text('chinese'), parse_mode="Markdown")
            await safe_send_message(context, uid, generate_help_text('myanmar'), parse_mode="Markdown")
        elif text == "自助续费":
            renew_msg = (
                f"💰 <b>【多群记账系统自动套餐购买中心】</b>\n\n"
                f"🔴 <b>1 个月商用续费：</b> <code>{PRICE_1_MONTH} USDT</code>\n"
                f"🟡 <b>2 个月商用续费：</b> <code>{PRICE_2_MONTH} USDT</code>\n"
                f"🟢 <b>3 个月商用续费：</b> <code>{PRICE_3_MONTH} USDT</code>\n\n"
                f"📌 <b>官方专属收币 TRC-20 地址：</b>\n👉 <code>{TRON_ADDRESS}</code>\n\n"
                f"💡 <i>转账完成后请【直接在这里发送支付成功截图】，系统会自动转交创始人进行秒级审批。</i>"
            )
            await safe_send_message(context, uid, renew_msg, parse_mode="HTML")
        elif text == "如何设置权限人":
            await safe_send_message(context, uid, "👑 <b>添加二级主人权限：</b>\n\n私聊发送指令：`指派二级主人 12345678` (后面换成目标用户的纯数字UID)\n\n*(每个买家最多支持添加 5 个协助二级主人)*", parse_mode="Markdown")
        elif text == "取掉权限人":
            if not (uid in FOUNDER_USERS or is_vip_user(uid)):
                await safe_send_message(context, uid, "❌ 您当前没有订购商用套餐，无权管理分销人。")
                return
            masters = get_dynamic_masters_by_creator(uid)
            if not masters:
                await safe_send_message(context, uid, "💡 <b>您目前还没有指派过任何二级新主人。</b>", parse_mode="HTML")
                return
            tips = "🗑️ <b>【撤销二级新主人特权中心】</b>\n\n发送下方对应的完整格式指令即可踢出授权：\n\n"
            for m_uid, m_name in masters:
                tips += f"👤 UID: <code>{m_uid}</code>\n👉 复制指令：`解除二级主人 {m_uid}`\n--------------------\n"
            await safe_send_message(context, uid, tips, parse_mode="HTML")
        elif text.startswith("解除二级主人"):
            if not (uid in FOUNDER_USERS or is_vip_user(uid)): return
            clean_uid = "".join(filter(str.isdigit, text))
            if len(clean_uid) >= 5:
                t_mid = int(clean_uid)
                conn = get_db_connection()
                c = conn.cursor()
                if uid in FOUNDER_USERS: c.execute("DELETE FROM dynamic_masters WHERE user_id = ?", (t_mid,))
                else: c.execute("DELETE FROM dynamic_masters WHERE user_id = ? AND added_by = ?", (t_mid, uid))
                conn.commit()
                conn.close()
                await safe_send_message(context, uid, f"🔥 <b>成功剥夺！二级新主人 (UID: {t_mid}) 的所有管理权限已被彻底清除。</b>", parse_mode="HTML")
        elif text.startswith("指派二级主人"):
            if not (uid in FOUNDER_USERS or is_vip_user(uid)): return
            if len(get_dynamic_masters_by_creator(uid)) >= 5 and uid not in FOUNDER_USERS:
                await safe_send_message(context, uid, "⚠️ <b>添加失败：您的二级主人添加名额已经达到5人天花板限制！</b>")
                return
            clean_uid = "".join(filter(str.isdigit, text))
            if len(clean_uid) >= 5:
                t_mid = int(clean_uid)
                conn = get_db_connection()
                c = conn.cursor()
                c.execute("INSERT OR REPLACE INTO dynamic_masters (user_id, username, added_by) VALUES (?, '授权二级主人', ?)", (t_mid, uid))
                conn.commit()
                conn.close()
                await safe_send_message(context, uid, f"✅ <b>指派成功！二级主人 (UID: {t_mid}) 已获得分销系统协同管理特权。</b>", parse_mode="HTML")
        elif text == "开启/关闭计算功能":
            await safe_send_message(context, uid, "💡 群内发送 <code>上课</code> 开启记账计算，发送 <code>下课</code> 锁定清算本日账目并扎帐。")
        return

    # --- 群组内到期判定 ---
    is_valid, _ = check_group_validity(gid, uid)
    if not is_valid:
        try: await context.bot.send_message(chat_id=uid, text="⚠️ <b>您的多群独立授权已到期，请您续费后使用机器人。</b>", parse_mode="HTML")
        except: pass
        return

    tz_str = get_setting(gid, 'timezone') or 'Asia/Shanghai'
    now, _, _ = get_current_time(tz_str)
    today_str = now.strftime("%Y-%m-%d")

    # ==================== 删除指令网关 ====================
    if text in ['删今天', '删明天']:
        if not can_use(gid, uid): return
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("DELETE FROM bills WHERE group_id = ? AND date_str = ?", (gid, today_str))
        conn.commit()
        conn.close()
        await safe_send_message(context, gid, "🧹 <b>清理完毕：今日在本群记录的所有账单流水已被全部抹除！</b>", parse_mode="HTML")
        await send_text_bill_report(update, gid, today_str, context)
        return

    if text == '全部清单':
        if not can_use(gid, uid): return
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("DELETE FROM bills WHERE group_id = ?", (gid,))
        conn.commit()
        conn.close()
        await safe_send_message(context, gid, "🚨 <b>历史大扫除完成：本群自入群以来的全部账单记录已被彻底清空！</b>", parse_mode="HTML")
        await send_text_bill_report(update, gid, today_str, context)
        return

    if text.startswith('清单+'):
        if not can_use(gid, uid): return
        target_remark = text.replace('清单+', '').strip()
        if not target_remark: return
        
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM bills WHERE group_id = ? AND date_str = ? AND remark = ? AND bill_type = 'income'", 
                  (gid, today_str, target_remark))
        count = c.fetchone()[0]
        if count > 0:
            c.execute("DELETE FROM bills WHERE group_id = ? AND date_str = ? AND remark = ? AND bill_type = 'income'", 
                      (gid, today_str, target_remark))
            conn.commit()
            conn.close()
            await safe_send_message(context, gid, f"🔥 <b>成功清理！已抹除今天备注为 [{target_remark}] 的所有进单记录（共计 {count} 笔）。</b>", parse_mode="HTML")
            await send_text_bill_report(update, gid, today_str, context)
        else:
            conn.close()
            await safe_send_message(context, gid, f"🔍 <b>未找到今天备注为 [{target_remark}] 的任何入款记录。</b>", parse_mode="HTML")
        return

    # ==================== 群内基础管理指令 ====================
    if text == '上课':
        if not can_use(gid, uid): return
        update_setting(gid, 'is_active', 1)
        await safe_send_message(context, gid, "🟢 <b>记账安全通道已开启！请开始录入账单。</b>", parse_mode="HTML")
        return

    if text == '下课':
        if not can_use(gid, uid): return
        update_setting(gid, 'is_active', 0)
        await safe_send_message(context, gid, "🔴 <b>下课成功！今日账单已自动封存锁定归档。</b>", parse_mode="HTML")
        await send_text_bill_report(update, gid, today_str, context)
        return

    if text.startswith('设置汇率'):
        if not can_use(gid, uid): return
        try:
            rate_val = float(text.replace('设置汇率', '').strip())
            update_setting(gid, 'exchange_rate', rate_val)
            await safe_send_message(context, gid, f"💱 <b>汇率修改成功！当前群常规汇率已变更为：【{rate_val:.2f}】</b>", parse_mode="HTML")
        except: pass
        return

    if text.startswith('设置操作人'):
        if not (is_master(uid) or is_vip_user(uid)): return
        t_id, show_name = None, None
        match = re.search(r'@(\w+)', text)
        if match: t_id, show_name = get_user_id_by_username(match.group(1))
        if not t_id and update.message.reply_to_message:
            t_id = update.message.reply_to_message.from_user.id
            u_obj = update.message.reply_to_message.from_user
            show_name = f"@{u_obj.username}" if u_obj.username else u_obj.first_name
        if t_id:
            ops = json.loads(get_setting(gid, 'operators') or '[]')
            if t_id not in ops: ops.append(t_id)
            update_setting(gid, 'operators', json.dumps(ops))
            await safe_send_message(context, gid, f"✅ <b>已成功将群成员 {show_name or t_id} 提拔为本群官方操作人。</b>", parse_mode="HTML")
        return

    if text == '改语言':
        if not can_use(gid, uid): return
        current_lang = get_setting(gid, 'language') or 'chinese'
        new_lang = 'myanmar' if current_lang == 'chinese' else 'chinese'
        update_setting(gid, 'language', new_lang)
        lang_tips = "🇲🇲 ระบบภาษาเปลี่ยนเป็น : พม่า" if new_lang == 'myanmar' else "🇨🇳 系统语言已切换为：中文 (Chinese)"
        await safe_send_message(context, gid, f"<b>{lang_tips}</b>", parse_mode="HTML")
        return

    if text == '删最后':
        if not can_use(gid, uid): return
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT id, remark, amount, bill_type, date_str FROM bills WHERE group_id = ? ORDER BY id DESC LIMIT 1", (gid,))
        row = c.fetchone()
        if row:
            b_id, b_rem, b_amt, b_type, b_date = row
            c.execute("DELETE FROM bills WHERE id = ?", (b_id,))
            conn.commit()
            conn.close()
            await safe_send_message(context, gid, f"🗑️ <b>已成功撤销最后一笔账单流水。</b>", parse_mode="HTML")
            await send_text_bill_report(update, gid, b_date, context)
        else: conn.close()
        return

    # ==================== 账目输入拦截流 ====================
    if (get_setting(gid, 'is_active') or 0) == 0: return
    if not can_use(gid, uid): return

    if text == '+0':
        await send_text_bill_report(update, gid, today_str, context)
        return

    # 🌟 无论输入什么记账指令，100% 回复以前最完整、带有明细和大按钮的长账单表格
    m_exp = re.match(r'^(.*?)(?:下发|ထုတ်)\s*(-?\d+(?:\.\d+)?)$', text)
    if m_exp:
        add_bill(gid, uid, username, m_exp.group(1).strip(), float(m_exp.group(2)), 'expense')
        await send_text_bill_report(update, gid, today_str, context)
        return

    m_inc = re.match(r'^(.*?)([\+\-])(\d+(?:\.\d+)?)(?:/(\d+(?:\.\d+)?))?$', text)
    if m_inc:
        rem = m_inc.group(1).strip()
        sign = m_inc.group(2)
        amt = float(m_inc.group(3))
        if sign == '-': amt = -amt
        c_rate = float(m_inc.group(4)) if m_inc.group(4) else None
        add_bill(gid, uid, username, rem, amt, 'income', c_rate)
        await send_text_bill_report(update, gid, today_str, context)
        return

# ==================== 买家上交截图审核网关 ====================
async def handle_photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private": return
    uid = update.effective_user.id
    photo_id = update.message.photo[-1].file_id
    
    app_k = [
        [InlineKeyboardButton("✅ 批准 1个月", callback_data=f"v_approve_{uid}_1"), InlineKeyboardButton("✅ 批准 2个月", callback_data=f"v_approve_{uid}_2")],
        [InlineKeyboardButton("✅ 批准 3个月", callback_data=f"v_approve_{uid}_3"), InlineKeyboardButton("❌ 驳回", callback_data=f"v_reject_{uid}")]
    ]
    for f_id in FOUNDER_USERS:
        try:
            await context.bot.send_photo(
                chat_id=f_id, photo=photo_id, 
                caption=f"📸 <b>报告老板，有买家提交转账截图啦！</b>\n\n买家UID: <code>{uid}</code>\n买家用户名: @{update.effective_user.username or '无'}", 
                reply_markup=InlineKeyboardMarkup(app_k), parse_mode="HTML"
            )
            await asyncio.sleep(1.0)
        except: pass
    await safe_send_message(context, uid, "📥 <b>您的入账转账截图已经提交至后台审核系统，请等待开通提示！</b>", parse_mode="HTML")

def main():
    init_db()
    threading.Thread(target=lambda: flask_app.run(host='0.0.0.0', port=PORT), daemon=True).start()
    
    # 核心高并发异步引擎调优参数：允许同时处理多群多命令（True）
    app = Application.builder().token(TOKEN).concurrent_updates(True).build()
    
    # 🌟 注册进群监听网关句柄
    app.add_handler(ChatMemberHandler(on_bot_join_group, ChatMemberHandler.MY_CHAT_MEMBER))
    
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CallbackQueryHandler(handle_callback_query))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo_message))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    print("🤖 全新自动进群致谢欢迎 + 纯净完整长账单版机器人启动就绪...")
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
