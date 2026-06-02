import os
import re
import json
import sqlite3
import logging
import threading
import asyncio
import random
from datetime import datetime, timedelta
import pytz
from flask import Flask, request, jsonify
import telebot
import requests  # 👈 已正确引入并集成，用于请求波场区块链网关

# ==================== 1. 系统核心配置 ====================
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# 自动从 Render 环境变量中读取配置
TOKEN = os.environ.get('TELEGRAM_TOKEN', '8617895746:AAEK4-npKZkbYCtTTkHITfmE9tmiJuWHG7k')
WEBHOOK_URL = os.environ.get('WEBHOOK_URL', 'https://shishi-668gg.onrender.com')
PORT = int(os.environ.get('PORT', 5000))

# 创始人 UID (拥有最高管理权限)
FOUNDER_USERS = [8179896441]
# 自助续费收款地址
TRON_ADDRESS = "TVnjLwDrGjYVRTa1ukfoE2mFTmCxtrjoCw"
# 商业化套餐定价 (USDT)
PRICE_1_MONTH = 80
PRICE_2_MONTH = 130
PRICE_3_MONTH = 220

bot = telebot.TeleBot(TOKEN, parse_mode=None)
flask_app = Flask(__name__)

# ==================== 2. 🌐 区块链波场链上数据抓取引擎 ====================
def fetch_blockchain_usdt_info(address):
    """
    通过 Tronscan 公共网关实时抓取指定地址的 USDT 余额和最近 5 笔转账流水
    """
    USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"  # 波场 USDT 官方智能合约地址
    try:
        # 1. 抓取该地址的全部代币资产余额
        account_url = f"https://apilist.tronscanapi.com/api/account/tokens?address={address}"
        response = requests.get(account_url, timeout=10)
        
        usdt_balance = 0.0
        if response.status_code == 200:
            data = response.json()
            for token in data.get('data', []):
                if token.get('tokenId') == USDT_CONTRACT:
                    usdt_balance = float(token.get('amount', 0)) / 1000000  # 精度除以 10^6
                    break
                    
        # 2. 抓取该地址最近的 TRC20 (USDT) 转账流水明细
        tx_url = f"https://apilist.tronscanapi.com/api/token_trc20/transfers?limit=5&start=0&sort=-timestamp&contract_address={USDT_CONTRACT}&relatedAddress={address}"
        tx_response = requests.get(tx_url, timeout=10)
        
        history_text = ""
        if tx_response.status_code == 200:
            tx_data = tx_response.json()
            tx_list = tx_data.get('token_transfers', [])
            
            if not tx_list:
                history_text = "  暂无最近的 USDT 转账流水。"
            else:
                for tx in tx_list:
                    from_addr = tx.get('from_address', '')
                    to_addr = tx.get('to_address', '')
                    amount = float(tx.get('quant', 0)) / 1000000
                    
                    # 判断是进账还是出账，并对地址进行脱敏美化处理
                    if from_addr.lower() == address.lower():
                        direction = "🔴 支出"
                        peer_info = f"去往: {to_addr[:6]}***{to_addr[-6:]}"
                    else:
                        direction = "🟢 收入"
                        peer_info = f"来自: {from_addr[:6]}***{from_addr[-6:]}"
                        
                    history_text += f"  {direction} | <b>{amount:.2f} U</b>\n  └ <i>{peer_info}</i>\n"
        else:
            history_text = "  ⚠️ 暂时无法获取流水明细，请稍后再试。"
            
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

def get_current_time(timezone_str):
    try:
        tz = pytz.timezone(timezone_str)
        now = datetime.now(tz)
        return now, now.strftime("%H:%M:%S"), now.strftime("%Y-%m-%d %H:%M:%S")
    except:
        tz = pytz.timezone('Asia/Shanghai')
        now = datetime.now(tz)
        return now, now.strftime("%H:%M:%S"), now.strftime("%Y-%m-%d %H:%M:%S")

# ==================== 4. ⚙️ 权限与配置逻辑 ====================
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
    if user_id and is_master(user_id): return True, "MASTER_BYPASS"
    if user_id and is_vip_user(user_id): return True, "VIP_DIRECT_PASS"
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
        c.execute(f"UPDATE settings SET {key} = ? WHERE group_id = ?", (value, group_id))
        conn.commit()
        conn.close()
    except: pass

# ==================== 5. 📊 记账数据业务层 ====================
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

def send_text_bill_report(chat_id, gid, target_date):
    rate = get_setting(gid, 'exchange_rate') or 7.2
    income, expense, total_income, total_expense = get_class_bills_by_date(gid, target_date)

    total_rmb = total_income[0] if (total_income and total_income[0]) else 0
    total_usdt = total_income[1] if (total_income and total_income[1]) else 0
    expense_usdt = total_expense[0] if (total_expense and total_expense[0]) else 0
    remaining_usdt = total_usdt - expense_usdt

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
    report += f"\n\n<code>[核算编号: {random.randint(1000,9999)}]</code>"

    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("📊 查看完整网页账单", url=f"{WEBHOOK_URL}?group_id={gid}"))
    bot.send_message(chat_id, report, parse_mode="HTML", reply_markup=markup)

# ==================== 6. 💬 Telegram 消息事件网关 ====================
@bot.message_handler(commands=['start', 'help'])
def cmd_start(message):
    gid = message.chat.id
    if message.chat.type == "private":
        welcome = "<b>欢迎使用多群分布式商用记账系统</b>\n\n直接将机器人拉入您的业务群组即可开始。群组发送 <code>上课</code> 启动录入。\n输入：<code>查看 Txxxx...</code> 可实时检索链上钱包。"
        bot.send_message(gid, welcome, parse_mode="HTML")
    else:
        check_group_validity(gid)
        bot.send_message(gid, "📊 多群分布式智能记账核算核心已部署就绪！输入 <code>上课</code> 启动。")

@bot.message_handler(func=lambda m: True)
def handle_all_messages(message):
    text = message.text.strip()
    gid = message.chat.id
    uid = message.from_user.id
    username = message.from_user.first_name or "记账员"

    # 🌟【核心新增功能】检测“查看 地址”指令（不限群组状态，完全独立运行）
    if text.startswith("查看"):
        parts = text.split()
        if len(parts) >= 2:
            target_address = parts[1].strip()
            if target_address.startswith("T") and len(target_address) == 34:
                wait_msg = bot.reply_to(message, "🔍 正在连接波场TRON全节点检索链上实时资产，请稍候...")
                
                # 请求波场链上 API
                chain_res = fetch_blockchain_usdt_info(target_address)
                
                try: bot.delete_message(gid, wait_msg.message_id)
                except: pass
                
                if chain_res["success"]:
                    report_text = (
                        f"👤 <b>查询目标地址：</b>\n<code>{target_address}</code>\n\n"
                        f"💰 <b>USDT 当前余额：</b> <code>{chain_res['balance']:.2f}</code> U\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"📊 <b>最近 5 笔链上流向明细：</b>\n\n{chain_res['history']}"
                    )
                    bot.reply_to(message, report_text, parse_mode="HTML")
                else:
                    bot.reply_to(message, f"❌ 链上检索失败，原因：{chain_res['msg']}")
                return
            else:
                bot.reply_to(message, "⚠️ 地址格式不规范，波场（TRC-20）地址通常为大写字母 T 开头的 34 位字符串。")
                return

    # 群内记账逻辑
    if message.chat.type in ["group", "supergroup"]:
        is_valid, _ = check_group_validity(gid, uid)
        if not is_valid: return
        
        tz_str = get_setting(gid, 'timezone') or 'Asia/Shanghai'
        now, _, _ = get_current_time(tz_str)
        today_str = now.strftime("%Y-%m-%d")

        if text == '上课':
            if not can_use(gid, uid): return
            update_setting(gid, 'is_active', 1)
            bot.reply_to(message, "🟢 记账通道已开启！可以开始录入账单。")
            return
        if text == '下课':
            if not can_use(gid, uid): return
            update_setting(gid, 'is_active', 0)
            bot.reply_to(message, "🔴 下课成功！今日账单已自动封存锁定。")
            send_text_bill_report(gid, gid, today_str)
            return

        if (get_setting(gid, 'is_active') or 0) == 0: return
        if not can_use(gid, uid): return

        if text == '+0':
            send_text_bill_report(gid, gid, today_str)
            return

        # 解析下发：备注下发100 或 下发100
        m_exp = re.match(r'^(.*?)(?:下发|ထုတ်)\s*(-?\d+(?:\.\d+)?)$', text)
        if m_exp:
            add_bill(gid, uid, username, m_exp.group(1).strip(), float(m_exp.group(2)), 'expense')
            send_text_bill_report(gid, gid, today_str)
            return

        # 解析入款：备注+1000 或 +1000/7.3
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

# ==================== 7. 🌐 Web 前端看板与 API 接口 ====================
@flask_app.route('/')
def index():
    return '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>分布式网页对账看板</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; font-family: sans-serif; }
        body { background-color: #f4f6f9; color: #333; padding: 12px; }
        .container { max-width: 800px; margin: 0 auto; background: #fff; border-radius: 12px; padding: 16px; box-shadow: 0 4px 12px rgba(0,0,0,0.05); }
        .header { text-align: center; margin-bottom: 20px; border-bottom: 2px solid #edf2f7; padding-bottom: 10px; }
        .summary-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; margin-bottom: 20px; }
        .card { background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 12px; text-align: center; }
        .card .title { font-size: 12px; color: #64748b; }
        .card .value { font-size: 18px; font-weight: bold; }
        table { width: 100%; border-collapse: collapse; margin-top: 10px; font-size: 13px; }
        th, td { padding: 10px; border-bottom: 1px solid #e2e8f0; text-align: left; }
        th { background: #f1f5f9; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h2>📊 实时分布式对账看板</h2>
            <p id="date-text">数据加载中...</p>
        </div>
        <div class="summary-grid">
            <div class="card"><div class="title">常规汇率</div><div class="value" id="rate">0.00</div></div>
            <div class="card"><div class="title">总入款 (RMB)</div><div class="value" id="total_rmb">0</div></div>
            <div class="card" style="background:#f0fdf4;"><div class="title">总入款 (USDT)</div><div class="value" style="color:#16a34a;" id="total_usdt">0U</div></div>
            <div class="card" style="background:#fef2f2;"><div class="title">已下发 (USDT)</div><div class="value" style="color:#dc2626;" id="expense_usdt">0U</div></div>
            <div class="card" style="grid-column: span 2; background:#eff6ff;"><div class="title">未下发尾款 (USDT)</div><div class="value" style="color:#1d4ed8; font-size:22px;" id="remaining_usdt">0U</div></div>
        </div>
        <h3>📥 进单明细</h3>
        <table>
            <thead><tr><th>时间</th><th>备注</th><th>金额(RMB)</th><th>折合(U)</th></tr></thead>
            <tbody id="income-list"></tbody>
        </table>
    </div>
    <script>
        async function loadBills() {
            const params = new URLSearchParams(window.location.search);
            const groupId = params.get('group_id') || '0';
            const response = await fetch(`/api/bill?group_id=${groupId}`);
            const data = await response.json();
            
            document.getElementById('rate').innerText = data.exchange_rate;
            document.getElementById('total_rmb').innerText = data.total_rmb;
            document.getElementById('total_usdt').innerText = data.total_usdt + ' U';
            document.getElementById('expense_usdt').innerText = data.expense_usdt + ' U';
            document.getElementById('remaining_usdt').innerText = data.remaining_usdt + ' U';
            document.getElementById('date-text').innerText = '当前群组: ' + groupId;
            
            const incBody = document.getElementById('income-list');
            if(data.income_bills && data.income_bills.length > 0) {
                incBody.innerHTML = data.income_bills.map(b => `
                    <tr><td>${b.time}</td><td><b>${b.remark}</b></td><td>+${b.amount}</td><td style="color:#16a34a;font-weight:bold;">${b.usdt} U</td></tr>
                `).join('');
            } else {
                incBody.innerHTML = '<tr><td colspan="4" style="text-align:center;color:#94a3b8;">暂无记录</td></tr>';
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
        
        income_bills = [{'remark': r[0] or '-', 'username': r[1] or '未知', 'amount': f"{r[2]:.0f}", 'usdt': f"{r[3]:.2f}", 'time': r[5][11:19] if r[5] else ''} for r in income]
        expense_bills = [{'remark': r[0] or '-', 'username': r[1] or '未知', 'usdt': f"{r[2]:.2f}", 'time': r[4][11:19] if r[4] else ''} for r in expense]
        
        return jsonify({
            'exchange_rate': f"{rate:.2f}",
            'total_rmb': f"{total_rmb:.0f}",
            'total_usdt': f"{total_usdt:.2f}",
            'expense_usdt': f"{expense_usdt:.2f}",
            'remaining_usdt': f"{total_usdt - expense_usdt:.2f}",
            'income_bills': income_bills,
            'expense_bills': expense_bills
        })
    except Exception as e:
        return jsonify({'error': True, 'msg': str(e)}), 500

@flask_app.route('/' + TOKEN, methods=['POST'])
def getMessage():
    json_string = request.get_data().decode('utf-8')
    update = telebot.types.Update.de_json(json_string)
    bot.process_new_updates([update])
    return "!", 200

# ==================== 8. 🚀 系统启动入口 ====================
if __name__ == '__main__':
    init_db()
    
    # 绑定 Webhook
    bot.remove_webhook()
    bot.set_webhook(url=f"{WEBHOOK_URL}/{TOKEN}")
    
    print(f"✅ Web对账看板与区块链底层网关成功融合。端口: {PORT}")
    flask_app.run(host='0.0.0.0', port=PORT)
