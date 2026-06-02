import os
import re
import json
import sqlite3
import logging
import random
from datetime import datetime, timedelta
import pytz
from flask import Flask, request, jsonify
import telebot
import requests

# ==================== 1. 系统核心配置 ====================
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

TOKEN = os.environ.get('TELEGRAM_TOKEN', '8617895746:AAHUyKA5aVC18VFXt5l9IWdhLs4oxNlvNaU')
WEBHOOK_URL = os.environ.get('WEBHOOK_URL', 'https://shishi-668gg.onrender.com')
PORT = int(os.environ.get('PORT', 5000))

FOUNDER_USERS = [8179896441]
TRON_ADDRESS = "TVnjLwDrGjYVRTa1ukfoE2mFTmCxtrjoCw"

bot = telebot.TeleBot(TOKEN, parse_mode=None)
flask_app = Flask(__name__)

# ==================== 2. 🌐 波场链上数据抓取引擎 ====================
def fetch_blockchain_usdt_info(address):
    USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
        'Accept': 'application/json'
    }
    try:
        # 修正后的 Tronscan 账户 Token 查询接口
        account_url = f"https://apilist.tronscanapi.com/api/account/tokens?address={address}"
        response = requests.get(account_url, headers=headers, timeout=10)
        usdt_balance = 0.0
        
        if response.status_code == 200:
            data = response.json()
            # 兼容处理返回的 token 列表结构
            token_list = data.get('data', data.get('tokens', []))
            for token in token_list:
                if token.get('tokenId') == USDT_CONTRACT or token.get('token_id') == USDT_CONTRACT:
                    raw_amount = token.get('balance', token.get('amount', 0))
                    usdt_balance = float(raw_amount) / 1000000.0
                    break
        else:
            return {"success": False, "msg": f"Tronscan 节点异常 ({response.status_code})"}
        
        # 获取转账流水的接口保持健壮性
        tx_url = f"https://apilist.tronscanapi.com/api/token_trc20/transfers?limit=5&start=0&sort=-timestamp&contract_address={USDT_CONTRACT}&relatedAddress={address}"
        history_text = ""
        try:
            tx_response = requests.get(tx_url, headers=headers, timeout=10)
            if tx_response.status_code == 200:
                tx_data = tx_response.json()
                tx_list = tx_data.get('token_transfers', tx_data.get('data', []))
                if not tx_list:
                    history_text = "  暂无最近的 USDT 转账流水。"
                else:
                    for tx in tx_list:
                        from_addr = tx.get('from_address', '')
                        to_addr = tx.get('to_address', '')
                        amount = float(tx.get('quant', tx.get('amount', 0))) / 1000000
                        if from_addr.lower() == address.lower():
                            direction = "🔴 支出"
                            peer_info = f"去往: {to_addr[:6]}***{to_addr[-6:]}"
                        else:
                            direction = "🟢 收入"
                            peer_info = f"来自: {from_addr[:6]}***{from_addr[-6:]}"
                        history_text += f"  {direction} | <b>{amount:.2f} U</b>\n  └ <i>{peer_info}</i>\n"
            else:
                history_text = "  ⚠️ 暂时无法获取流水明细（频率受限）。"
        except:
            history_text = "  ⚠️ 链上网络拥堵，流水加载失败。"

        return {"success": True, "balance": usdt_balance, "history": history_text}
    except Exception as e:
        return {"success": False, "msg": str(e)}

# ==================== 3. 💾 SQLite 数据库引擎 ====================
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
                  fee_rate REAL DEFAULT 0, is_active INTEGER DEFAULT 1, language TEXT DEFAULT 'chinese',
                  timezone TEXT DEFAULT 'Asia/Shanghai', show_usdt INTEGER DEFAULT 1, expire_time TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS bills
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER, user_id INTEGER, username TEXT,
                  remark TEXT, amount REAL, usdt_amount REAL, exchange_rate REAL, bill_type TEXT,
                  timestamp TEXT, date_str TEXT, is_settled INTEGER DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS vip_users
                 (user_id INTEGER PRIMARY KEY, username TEXT, expire_time TEXT)''')
    conn.commit()
    conn.close()

def get_current_time(timezone_str='Asia/Shanghai'):
    try:
        tz = pytz.timezone(timezone_str)
        now = datetime.now(tz)
        return now, now.strftime("%H:%M:%S"), now.strftime("%Y-%m-%d %H:%M:%S")
    except:
        tz = pytz.timezone('Asia/Shanghai')
        now = datetime.now(tz)
        return now, now.strftime("%H:%M:%S"), now.strftime("%Y-%m-%d %H:%M:%S")

def get_vip_expire_time(user_id):
    if user_id in FOUNDER_USERS: 
        return True, "永久终身授权"
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT expire_time FROM vip_users WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        conn.close()
        if row:
            expire = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
            if datetime.now() < expire:
                return True, row[0]
            else:
                return False, row[0]
    except: pass
    return False, "未激活"

def add_vip_months(user_id, username, months):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT expire_time FROM vip_users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    
    now = datetime.now()
    if row:
        try:
            current_expire = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
            base_time = current_expire if current_expire > now else now
        except:
            base_time = now
    else:
        base_time = now
        
    new_expire = base_time + timedelta(days=30 * months)
    expire_str = new_expire.strftime("%Y-%m-%d %H:%M:%S")
    
    c.execute("INSERT OR REPLACE INTO vip_users (user_id, username, expire_time) VALUES (?, ?, ?)",
              (user_id, username, expire_str))
    conn.commit()
    conn.close()
    return expire_str

def get_setting(group_id, key):
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT * FROM settings WHERE group_id = ?", (group_id,))
        row = c.fetchone()
        if not row:
            _, _, init_time = get_current_time()
            c.execute("INSERT OR IGNORE INTO settings (group_id, operators, exchange_rate, fee_rate, is_active, language, timezone, show_usdt, expire_time) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      (group_id, '[]', 7.2, 0, 1, 'chinese', 'Asia/Shanghai', 1, init_time))
            conn.commit()
            c.execute("SELECT * FROM settings WHERE group_id = ?", (group_id,))
            row = c.fetchone()
        conn.close()
        cols = ['group_id', 'operators', 'exchange_rate', 'fee_rate', 'is_active', 'language', 'timezone', 'show_usdt', 'expire_time']
        return dict(zip(cols, row)).get(key)
    except: return None

def update_setting(group_id, key, value):
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(f"UPDATE settings SET {key} = ? WHERE group_id = ?", (value, group_id))
        conn.commit()
        conn.close()
    except: pass

def is_operator(group_id, user_id):
    if user_id in FOUNDER_USERS: return True
    ops_str = get_setting(group_id, 'operators') or '[]'
    try:
        ops = json.loads(ops_str)
        return user_id in ops
    except: return False

# ==================== 4. 📊 记账数据业务层 ====================
def add_bill(group_id, user_id, username, remark, amount, bill_type, exchange_rate=None):
    if exchange_rate is None:
        exchange_rate = get_setting(group_id, 'exchange_rate') or 7.2
    if bill_type == 'income':
        usdt_amount = amount / exchange_rate
    else:
        usdt_amount = amount
    _, _, full_time = get_current_time()
    date_str = full_time[:10]

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

def send_text_bill_report(chat_id, gid, target_date):
    rate = get_setting(gid, 'exchange_rate') or 7.2
    income, expense, total_income, total_expense = get_class_bills_by_date(gid, target_date)
    total_rmb = total_income[0] if (total_income and total_income[0]) else 0
    total_usdt = total_income[1] if (total_income and total_income[1]) else 0
    expense_usdt = total_expense[0] if (total_expense and total_expense[0]) else 0
    remaining_usdt = total_usdt - expense_usdt

    report = f"📊 <b>账单汇总 ({target_date})</b>\n\n📥 <b>入款 (最后5笔):</b>\n"
    if income:
        for row in income[-5:]:
            remark, username, amount, usdt_amount, ex_rate, timestamp = row
            time_str = timestamp[11:16] if timestamp else "00:00"
            report += f"  {time_str} {amount:.0f}/{ex_rate:.2f}= {usdt_amount:.1f}U" + (f" ({remark})\n" if remark else "\n")
    else:
        report += "  暂无入款数据\n"
    if expense:
        report += "\n📤 <b>下发 (最后5笔):</b>\n"
        for row in expense[-5:]:
            remark, username, usdt_amount, ex_rate, timestamp = row
            time_str = timestamp[11:16] if timestamp else "00:00"
            report += f"  {time_str} 下发 {usdt_amount:.1f}U" + (f" ({remark})\n" if remark else "\n")

    report += f"\n💰 <b>汇率:</b> {rate:.2f}\n📊 <b>总入款:</b> {total_rmb:.0f} | {total_usdt:.1f}U\n📊 <b>已下发:</b> {expense_usdt:.1f}U\n📊 <b>未下发:</b> {remaining_usdt:.1f}U\n\n<code>[核算编号: {random.randint(1000,9999)}]</code>"
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("📊 查看完整网页账单", url=f"{WEBHOOK_URL}?group_id={gid}"))
    bot.send_message(chat_id, report, parse_mode="HTML", reply_markup=markup)

# ==================== 5. 💬 Telegram 核心控制指令扩展网关 ====================
@bot.message_handler(commands=['start', 'help'])
def cmd_start(message):
    gid = message.chat.id
    welcome = (
        "🤖 <b>小跟班智能分布式记账系统已激活</b>\n\n"
        "👉 <b>群内核心记账命令：</b>\n"
        "• 发送 <code>上课</code> / <code>下课</code> 开启或封存账单\n"
        "• 发送 <code>+1000</code> 或 <code>+1000/7.3</code> 记入款\n"
        "• 发送 <code>项目公款+5000</code> 记带备注账目\n"
        "• 发送 <code>下发500</code> 记下发\n"
        "• 发送 <code>+0</code> 查看对账大底\n\n"
        "⚙️ <b>高阶财务群管命令（仅限操作人）：</b>\n"
        "• <code>设置汇率 7.35</code> - 一键调整当前汇率\n"
        "• <code>设置操作人 @username</code> - 授权他人共同记账管理\n"
        "• <code>清单 备注名</code> - 过滤查询指定备注名下的所有进单明细\n"
        "• <code>删最后</code> - 撤销最后一笔错误记账\n"
        "• <code>删今天</code> - 清空今天的所有账单记录\n"
        "• <code>删全部</code> - 清空本群历史所有未清算数据"
    )
    bot.send_message(gid, welcome, parse_mode="HTML")

@bot.message_handler(content_types=['photo'])
def handle_receipt_photo(message):
    if message.chat.type != "private": return 
    uid = message.from_user.id
    username = message.from_user.username or "无用户名"
    first_name = message.from_user.first_name or "买家"
    photo_id = message.photo[-1].file_id
    
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(
        telebot.types.InlineKeyboardButton("✅ 批准 1个月", callback_data=f"auth_1_{uid}_{username}"),
        telebot.types.InlineKeyboardButton("✅ 批准 3个月", callback_data=f"auth_3_{uid}_{username}")
    )
    markup.add(telebot.types.InlineKeyboardButton("❌ 驳回凭证", callback_data=f"auth_reject_{uid}"))
    
    for founder in FOUNDER_USERS:
        try:
            bot.send_message(founder, f"🔔 <b>收到新的自助续费申请！</b>\n\n👤 买家: {first_name} (@{username})\n🆔 UID: <code>{uid}</code>", parse_mode="HTML")
            bot.send_photo(founder, photo_id, reply_markup=markup)
        except: pass
    bot.reply_to(message, "⏳ <b>凭证已成功提交！</b> 系统正在核验，请耐心等待 1-3 分钟。")

@bot.callback_query_handler(func=lambda call: call.data.startswith('auth_'))
def handle_auth_buttons(call):
    if call.from_user.id not in FOUNDER_USERS:
        bot.answer_callback_query(call.id, "⚠️ 您不是创始人，无权审核！", show_alert=True)
        return
    data_parts = call.data.split('_')
    action = data_parts[1]
    
    if action == "reject":
        buyer_id = int(data_parts[2])
        try: bot.send_message(buyer_id, "❌ <b>您的续费申请已被拒绝。</b>", parse_mode="HTML")
        except: pass
        bot.edit_message_caption("❌ 已驳回该买家的凭证申请。", chat_id=call.message.chat.id, message_id=call.message.message_id)
    else:
        months = int(action)
        buyer_id = int(data_parts[2])
        buyer_name = data_parts[3]
        expire_str = add_vip_months(buyer_id, buyer_name, months)
        try:
            bot.send_message(buyer_id, f"🎉 <b>您的自助续费申请已通过审核！开通 {months} 个月 VIP 授权。</b>", parse_mode="HTML")
        except: pass
        bot.edit_message_caption(f"✅ 审核成功！到期时间: {expire_str}", chat_id=call.message.chat.id, message_id=call.message.message_id)
    bot.answer_callback_query(call.id, "操作成功！")

@bot.message_handler(func=lambda m: True)
def handle_all_messages(message):
    text = message.text.strip()
    gid = message.chat.id
    uid = message.from_user.id
    username = message.from_user.first_name or "用户"

    # 问题 2：自助续费文案补充价格体系
    if text == "自助续费":
        reply = (
            f"💎 <b>官方波场(TRC20)收款地址：</b>\n<code>{TRON_ADDRESS}</code>\n\n"
            f"💰 <b>授权价格套餐：</b>\n"
            f"• 1 个月：<b>80</b> RMB / U\n"
            f"• 2 个月：<b>140</b> RMB / U\n"
            f"• 3 个月：<b>230</b> RMB / U\n\n"
            f"⚠️ 转账后直接把【交易成功截图】发送给机器人即可完成自动申请核验。"
        )
        bot.reply_to(message, reply, parse_mode="HTML")
        return

    if text == "详细说明书":
        bot.reply_to(message, "📖 请发送 /help 查看完整的功能命令帮助指南。")
        return

    # 问题 3：到期时间加入精确时间展示
    if text == "到期时间":
        is_active, expire_time = get_vip_expire_time(uid)
        status = "🟢 VIP 激活中" if is_active else "🔴 已到期/未激活"
        bot.reply_to(message, f"👤 <b>权限状态</b>: {status}\n📅 <b>到期时间</b>: <code>{expire_time}</code>", parse_mode="HTML")
        return

    # 🔍 链上实时查询
    if text.startswith("查看"):
        parts = text.split()
        if len(parts) >= 2:
            target_address = parts[1].strip()
            if target_address.startswith("T") and len(target_address) == 34:
                wait_msg = bot.reply_to(message, "🔍 正在连接波场TRON全节点检索实时资产...")
                chain_res = fetch_blockchain_usdt_info(target_address)
                try: bot.delete_message(gid, wait_msg.message_id)
                except: pass
                if chain_res["success"]:
                    report_text = f"👤 <b>查询地址：</b>\n<code>{target_address}</code>\n\n💰 <b>USDT 当前余额：</b> <code>{chain_res['balance']:.2f}</code> U\n━━━━━━━━━━━━━━━━━━\n📊 <b>流向明细：</b>\n{chain_res['history']}"
                    bot.reply_to(message, report_text, parse_mode="HTML")
                else:
                    bot.reply_to(message, f"❌ 检索失败: {chain_res['msg']}")
                return

    # =============== 以下功能仅限在群组内使用且需要授权 ===============
    if message.chat.type in ["group", "supergroup"]:
        is_active, _ = get_vip_expire_time(uid)
        if not is_active: return 
        now, _, _ = get_current_time()
        today_str = now.strftime("%Y-%m-%d")

        # 1️⃣ 设置汇率功能
        if text.startswith("设置汇率"):
            if not is_operator(gid, uid):
                bot.reply_to(message, "⚠️ 只有群操作人或老板才能修改汇率。")
                return
            try:
                new_rate = float(text.replace("设置汇率", "").strip())
                update_setting(gid, 'exchange_rate', new_rate)
                bot.reply_to(message, f"✅ 汇率已成功调整为: <b>{new_rate:.2f}</b>", parse_mode="HTML")
            except:
                bot.reply_to(message, "❌ 格式错误！请输入如: `设置汇率 7.3`")
            return

        # 2️⃣ 设置操作人功能
        if text.startswith("设置操作人"):
            if uid not in FOUNDER_USERS:
                bot.reply_to(message, "⚠️ 只有创始人(老板)才能委任群操作人。")
                return
            if message.reply_to_message:
                target_id = message.reply_to_message.from_user.id
                target_name = message.reply_to_message.from_user.first_name
            else:
                bot.reply_to(message, "💡 请【回复】那个需要被设置为操作人的用户的消息，并发送 `设置操作人`")
                return
            try:
                ops_str = get_setting(gid, 'operators') or '[]'
                ops = json.loads(ops_str)
                if target_id not in ops:
                    ops.append(target_id)
                    update_setting(gid, 'operators', json.dumps(ops))
                bot.reply_to(message, f"✅ 已成功将 <b>{target_name}</b> 设为本群操作人！", parse_mode="HTML")
            except Exception as e:
                bot.reply_to(message, f"❌ 设置失败: {str(e)}")
            return

        # 3️⃣ 删最后 / 删今天 / 删全部功能
        if text in ["删最后", "删今天", "删全部"]:
            if not is_operator(gid, uid):
                bot.reply_to(message, "⚠️ 无权操作！只有操作人可以删账。")
                return
            conn = get_db_connection()
            c = conn.cursor()
            if text == "删最后":
                c.execute("SELECT id, remark, amount FROM bills WHERE group_id = ? ORDER BY id DESC LIMIT 1", (gid,))
                last_row = c.fetchone()
                if last_row:
                    c.execute("DELETE FROM bills WHERE id = ?", (last_row[0],))
                    bot.reply_to(message, f"🗑️ 已成功撤销最后一笔账目: 【{last_row[1] or '无备注'}: {last_row[2]}】")
                else:
                    bot.reply_to(message, "📭 当前没有任何账单记录。")
            elif text == "删今天":
                c.execute("DELETE FROM bills WHERE group_id = ? AND date_str = ?", (gid, today_str))
                bot.reply_to(message, f"🗑️ 已清空今日 ({today_str}) 的所有账单数据！")
            elif text == "删全部":
                c.execute("DELETE FROM bills WHERE group_id = ?", (gid,))
                bot.reply_to(message, "🗑️ 已清空本群历史所有的账单数据！")
            conn.commit()
            conn.close()
            send_text_bill_report(gid, gid, today_str)
            return

        # 4️⃣ 清单 + 备注 过滤查询功能
        if text.startswith("清单"):
            target_remark = text.replace("清单", "").strip()
            if not target_remark:
                bot.reply_to(message, "💡 请指定具体备注名，例如: `清单 飞机群公款`")
                return
            conn = get_db_connection()
            c = conn.cursor()
            c.execute("SELECT timestamp, amount, usdt_amount, exchange_rate FROM bills WHERE group_id = ? AND date_str = ? AND remark = ? AND bill_type='income'", (gid, today_str, target_remark))
            rows = c.fetchall()
            conn.close()
            if not rows:
                bot.reply_to(message, f"🔍 今日暂无带有备注【{target_remark}】的进单记录。")
            else:
                q_report = f"📋 <b>【{target_remark}】专属进单明细：</b>\n\n"
                total_r, total_u = 0, 0
                for r in rows:
                    time_s = r[0][11:16]
                    q_report += f"  🔹 {time_s} | 进 <b>{r[1]:.0f}</b> RMB -> 折合 <b>{r[2]:.1f}</b> U (汇率:{r[3]:.2f})\n"
                    total_r += r[1]
                    total_u += r[2]
                q_report += f"\n📈 <b>小计汇总：</b>\n总入款: {total_r:.0f} RMB\n总折合: {total_u:.1f} USDT"
                bot.reply_to(message, q_report, parse_mode="HTML")
            return

        # 基础上下课控制
        if text == '上课':
            update_setting(gid, 'is_active', 1)
            bot.reply_to(message, "🟢 记账安全通道已开启！")
            return
        if text == '下课':
            update_setting(gid, 'is_active', 0)
            bot.reply_to(message, "🔴 下课成功！今日账单已自动封存锁定。")
            send_text_bill_report(gid, gid, today_str)
            return

        if (get_setting(gid, 'is_active') or 0) == 0: return

        if text == '+0':
            send_text_bill_report(gid, gid, today_str)
            return

        m_exp = re.match(r'^(.*?)(?:下发|ထုတ်)\s*(-?\d+(?:\.\d+)?)$', text)
        if m_exp:
            add_bill(gid, uid, username, m_exp.group(1).strip(), float(m_exp.group(2)), 'expense')
            send_text_bill_report(gid, gid, today_str)
            return

        m_inc = re.match(r'^(.*?)([\+\-])(\d+(?:\.\d+)?)(?:/(\d+(?:\.\d+)?))?$', text)
        if m_inc:
            rem = m_inc.group(1).strip()
            sign = m_inc.group(2)
            amt = float(m_inc.group(3))
            if sign == '-': amt = -amt
            c_rate = float(m_inc.group(4)) if m_inc.group(4) else None
            add_bill(gid, uid, username, rem, amt, 'income', c_rate)
            send_text_bill_report(gid, gid, today_str)
            return

# ==================== 6. 🌐 Web 前端与 API 看板 ====================
# 问题 4：已完全对换网页布局，使明细在上，总计卡片在底部
@flask_app.route('/')
def index():
    return '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>分布式全功能网页账单</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; font-family: -apple-system, sans-serif; }
        body { background-color: #f4f6f9; color: #333; padding: 12px; line-height: 1.4; }
        .container { max-width: 800px; margin: 0 auto; background: #fff; border-radius: 12px; padding: 16px; box-shadow: 0 4px 12px rgba(0,0,0,0.05); }
        .header { text-align: center; margin-bottom: 20px; border-bottom: 2px solid #edf2f7; padding-bottom: 15px; }
        .date-picker-area { margin: 10px 0; background: #f8fafc; padding: 8px; border-radius: 6px; display: flex; align-items: center; justify-content: center; gap: 8px; border: 1px dashed #cbd5e1; }
        .date-picker-area label { font-size: 13px; font-weight: bold; color: #475569; }
        .date-input { padding: 6px 10px; border-radius: 4px; border: 1px solid #cbd5e1; font-size: 14px; color: #1e293b; outline: none; cursor: pointer; font-weight: bold; }
        .summary-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; margin-top: 25px; border-top: 2px dashed #cbd5e1; padding-top: 20px; }
        .card { background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 12px; text-align: center; }
        .card .title { font-size: 12px; color: #64748b; }
        .card .value { font-size: 18px; font-weight: bold; margin-top: 2px; }
        h3 { font-size: 15px; margin: 25px 0 8px 0; padding-left: 6px; border-left: 4px solid #3b82f6; color: #1e293b; }
        .cate-title { border-left-color: #10b981; }
        .exp-title { border-left-color: #ef4444; }
        table { width: 100%; border-collapse: collapse; margin-top: 5px; font-size: 13px; background: #fff; border-radius: 6px; overflow: hidden; }
        th, td { padding: 10px; border-bottom: 1px solid #e2e8f0; text-align: left; }
        th { background: #f1f5f9; color: #475569; font-weight: 600; }
        .badge { display: inline-block; padding: 2px 6px; font-size: 11px; border-radius: 4px; font-weight: bold; background: #e2e8f0; color: #475569; }
        .bg-inc { background: #dcfce7; color: #15803d; }
        .bg-exp { background: #fee2e2; color: #b91c1c; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h2>📊 分布式对账看板系统</h2>
            <p id="group-text" style="font-size:12px; color:#64748b; margin-top:4px;">正在载入群组数据...</p>
            <div class="date-picker-area">
                <label for="date-select">📅 账单转日查看：</label>
                <input type="date" id="date-select" class="date-input" onchange="dateChanged(this.value)">
            </div>
        </div>

        <!-- 1. 📥 进单明细 (移到最上方) -->
        <h3>📥 进单明细</h3>
        <table>
            <thead><tr><th>时间</th><th>备注项目</th><th>金额(RMB)</th><th>折合(U)</th></tr></thead>
            <tbody id="income-list"></tbody>
        </table>

        <!-- 2. 📤 下发记录明细 (移到第二) -->
        <h3 class="exp-title">📤 下发记录明细</h3>
        <table>
            <thead><tr><th>时间</th><th>下发备注</th><th>下发金额(USDT)</th><th>操作人</th></tr></thead>
            <tbody id="expense-list"></tbody>
        </table>

        <!-- 3. 🗂️ 备注分类统计 (移到第三) -->
        <h3 class="cate-title">🗂️ 备注分类统计</h3>
        <table>
            <thead><tr><th>项目备注</th><th>总金额(RMB)</th><th>折合(USDT)</th><th>笔数</th></tr></thead>
            <tbody id="cate-list"></tbody>
        </table>

        <!-- 4. 常规数据总计面板 (移到最下方) -->
        <div class="summary-grid">
            <div class="card"><div class="title">常规汇率</div><div class="value" id="rate">0.00</div></div>
            <div class="card"><div class="title">总入款 (RMB)</div><div class="value" id="total_rmb">0</div></div>
            <div class="card" style="background:#f0fdf4;"><div class="title">总入款 (USDT)</div><div class="value" style="color:#16a34a;" id="total_usdt">0U</div></div>
            <div class="card" style="background:#fef2f2;"><div class="title">已下发 (USDT)</div><div class="value" style="color:#dc2626;" id="expense_usdt">0U</div></div>
            <div class="card" style="grid-column: span 2; background:#eff6ff;"><div class="title">未下发尾款 (USDT)</div><div class="value" style="color:#1d4ed8; font-size:20px;" id="remaining_usdt">0U</div></div>
        </div>
    </div>
    <script>
        const params = new URLSearchParams(window.location.search);
        const groupId = params.get('group_id') || '0';
        document.getElementById('group-text').innerText = '当前查看群组ID: ' + groupId;

        if(!params.get('date')) {
            const today = new Date();
            const year = today.getFullYear();
            const month = String(today.getMonth() + 1).padStart(2, '0');
            const day = String(today.getDate()).padStart(2, '0');
            document.getElementById('date-select').value = `${year}-${month}-${day}`;
        } else {
            document.getElementById('date-select').value = params.get('date');
        }

        function dateChanged(newDate) {
            window.location.href = `?group_id=${groupId}&date=${newDate}`;
        }

        async function loadBills() {
            const currentDate = document.getElementById('date-select').value;
            try {
                const response = await fetch(`/api/bill?group_id=${groupId}&date=${currentDate}`);
                const data = await response.json();
                
                document.getElementById('rate').innerText = data.exchange_rate;
                document.getElementById('total_rmb').innerText = data.total_rmb;
                document.getElementById('total_usdt').innerText = data.total_usdt + ' U';
                document.getElementById('expense_usdt').innerText = data.expense_usdt + ' U';
                document.getElementById('remaining_usdt').innerText = data.remaining_usdt + ' U';
                
                const cateBody = document.getElementById('cate-list');
                if(data.category_summary && data.category_summary.length > 0) {
                    cateBody.innerHTML = data.category_summary.map(c => `<tr><td><span class="badge bg-inc">${c.remark}</span></td><td><b>${c.total_rmb}</b></td><td style="color:#16a34a;font-weight:bold;">${c.total_usdt} U</td><td>${c.count} 笔</td></tr>`).join('');
                } else {
                    cateBody.innerHTML = '<tr><td colspan="4" style="text-align:center;color:#94a3b8;">暂无分类统计记录</td></tr>';
                }

                const incBody = document.getElementById('income-list');
                if(data.income_bills && data.income_bills.length > 0) {
                    incBody.innerHTML = data.income_bills.map(b => `<tr><td>${b.time}</td><td><b>${b.remark}</b></td><td>+${b.amount}</td><td style="color:#16a34a;font-weight:bold;">${b.usdt} U</td></tr>`).join('');
                } else {
                    incBody.innerHTML = '<tr><td colspan="4" style="text-align:center;color:#94a3b8;">今日暂无入款明细</td></tr>';
                }
                
                const expBody = document.getElementById('expense-list');
                if(data.expense_bills && data.expense_bills.length > 0) {
                    expBody.innerHTML = data.expense_bills.map(e => `<tr><td>${e.time}</td><td><span class="badge bg-exp">${e.remark}</span></td><td style="color:#dc2626;font-weight:bold;">-${e.usdt} U</td><td>${e.username}</td></tr>`).join('');
                } else {
                    expBody.innerHTML = '<tr><td colspan="4" style="text-align:center;color:#94a3b8;">今日暂无下发流出</td></tr>';
                }
            } catch(e) {
                console.error("加载数据失败:", e);
            }
        }
        window.onload = loadBills;
    </script>
</body>
</html>'''

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
        
        total_rmb = total_income[0] if (total_income and total_income[0]) else 0
        total_usdt = total_income[1] if (total_income and total_income[1]) else 0
        expense_usdt = total_expense[0] if (total_expense and total_expense[0]) else 0
        
        income_bills = [{'remark': r[0] or '无备注', 'username': r[1] or '未知', 'amount': f"{r[2]:.0f}", 'usdt': f"{r[3]:.2f}", 'time': r[5][11:19] if r[5] else ''} for r in income]
        expense_bills = [{'remark': r[0] or '无备注', 'username': r[1] or '未知', 'usdt': f"{r[2]:.2f}", 'time': r[4][11:19] if r[4] else ''} for r in expense]
        
        summary_dict = {}
        for row in income:
            rem = row[0].strip() if row[0] else "空备注"
            amt = row[2] or 0.0
            u_amt = row[3] or 0.0
            if rem not in summary_dict:
                summary_dict[rem] = {"total_rmb": 0.0, "total_usdt": 0.0, "count": 0}
            summary_dict[rem]["total_rmb"] += amt
            summary_dict[rem]["total_usdt"] += u_amt
            summary_dict[rem]["count"] += 1
            
        category_summary = []
        for k, v in summary_dict.items():
            category_summary.append({
                "remark": k,
                "total_rmb": f"{v['total_rmb']:.0f}",
                "total_usdt": f"{v['total_usdt']:.2f}",
                "count": v["count"]
            })
            
        return jsonify({
            'exchange_rate': f"{rate:.2f}", 'total_rmb': f"{total_rmb:.0f}", 'total_usdt': f"{total_usdt:.2f}",
            'expense_usdt': f"{expense_usdt:.2f}", 'remaining_usdt': f"{total_usdt - expense_usdt:.2f}",
            'income_bills': income_bills, 'expense_bills': expense_bills,
            'category_summary': category_summary
        })
    except Exception as e:
        return jsonify({'error': True, 'msg': str(e)}), 500

@flask_app.route('/' + TOKEN, methods=['POST'])
def getMessage():
    json_string = request.get_data().decode('utf-8')
    update = telebot.types.Update.de_json(json_string)
    bot.process_new_updates([update])
    return "!", 200

if __name__ == '__main__':
    init_db()
    bot.remove_webhook()
    bot.set_webhook(url=f"{WEBHOOK_URL}/{TOKEN}")
    print(f"🚀 全功能分布式看板系统已启动！")
    flask_app.run(host='0.0.0.0', port=PORT)
