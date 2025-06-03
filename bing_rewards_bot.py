import json
import asyncio
import logging
import os.path
import re
import time

from playwright.sync_api import sync_playwright, Page, expect

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ロギングの設定
# ログをファイルとコンソールの両方に出力するように変更
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bing_rewards_bot.log', mode='a'), # ファイルへの出力
        logging.StreamHandler() # コンソールへの出力
    ]
)

def load_config(config_path='config.json'):
    """設定ファイルからログイン情報を読み込む"""
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)
        return config
    except FileNotFoundError:
        logging.error(f"設定ファイル '{config_path}' が見つかりません。")
        return None
    except json.JSONDecodeError:
        logging.error(f"設定ファイル '{config_path}' の形式が不正です。JSON形式であることを確認してください。")
        return None

# Gmail APIの認証とサービスオブジェクトの取得
SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']

def authenticate_gmail():
    creds = None
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                'credentials.json', SCOPES)
            creds = flow.run_local_server(port=0)
        with open('token.json', 'w') as token:
            token.write(creds.to_json())
    logging.info("Gmail APIの認証が完了しました。")
    return build('gmail', 'v1', credentials=creds)

# Gmailから認証コードを取得する関数
def get_verification_code_from_gmail(service, target_email, timeout=120):
    logging.info(f"Gmailから認証コードを検索中... (タイムアウト: {timeout}秒)")
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            # 最新のメールを検索 (maxResultsを増やし、より多くのメールを取得)
            query = f"from:account-security-noreply@accountprotection.microsoft.com subject:\"Microsoft アカウントのセキュリティ コード\""
            # maxResultsを増やし、最新のメールを確実に取得できるようにする
            results = service.users().messages().list(userId='me', q=query, maxResults=5).execute()
            messages = results.get('messages', [])

            if not messages:
                logging.info("認証コードのメールが見つかりません。10秒待機して再試行します。")
                time.sleep(10)
                continue

            # 取得したメッセージをinternalDateでソートし、最新のものを選択
            # internalDateはミリ秒単位のUNIXタイムスタンプ
            # messagesリストはIDのみを含むため、個々のメッセージを取得してinternalDateを確認する必要がある
            
            # まず、メッセージIDからメッセージの詳細を取得し、internalDateを付加する
            detailed_messages = []
            for msg_id_obj in messages:
                try:
                    msg_detail = service.users().messages().get(userId='me', id=msg_id_obj['id'], format='metadata', metadataHeaders=['internalDate']).execute()
                    # internalDateは文字列なのでintに変換
                    msg_detail['internalDate'] = int(msg_detail['internalDate'])
                    detailed_messages.append(msg_detail)
                except HttpError as e:
                    logging.warning(f"メッセージID {msg_id_obj['id']} の詳細取得中にエラー: {e}")
                    continue
            
            if not detailed_messages:
                logging.info("詳細を取得できる認証コードのメールが見つかりません。10秒待機して再試行します。")
                time.sleep(10)
                continue

            # internalDateで降順にソート（最新のものが最初に来るように）
            detailed_messages.sort(key=lambda x: x['internalDate'], reverse=True)
            
            # 最も新しいメールのIDを取得
            latest_message_id = detailed_messages[0]['id']
            
            msg = service.users().messages().get(userId='me', id=latest_message_id, format='full').execute()
            
            # メール本文の取得 (既存ロジック)
            payload = msg['payload']
            parts = payload.get('parts', [])
            body_data = ""
            if parts:
                for part in parts:
                    if part['mimeType'] == 'text/plain':
                        body_data = part['body']['data']
                        break
            else:
                body_data = payload['body']['data']

            # base64urlデコード
            import base64
            decoded_body = base64.urlsafe_b64decode(body_data).decode('utf-8')
            
            logging.debug(f"メール本文:\n{decoded_body}")

            # 正規表現でセキュリティコードを抽出
            match = re.search(r'セキュリティ コード: (\d{6})', decoded_body)
            if match:
                code = match.group(1)
                logging.info(f"認証コードが見つかりました: {code}")
                return code
            else:
                logging.warning("メール本文からセキュリティコードを抽出できませんでした。10秒待機して再試行します。")
                time.sleep(10)

        except HttpError as error:
            logging.error(f"Gmail APIエラーが発生しました: {error}")
            break
        except Exception as e:
            logging.error(f"認証コードの取得中に予期せぬエラーが発生しました: {e}", exc_info=True)
            break
    
    logging.error("認証コードの取得がタイムアウトしました。")
    return None


def run_bot():
    """Bing Rewardsの自動クリックボットを実行する"""
    config = load_config()
    if not config:
        logging.error("設定ファイルの読み込みに失敗しました。処理を終了します。")
        return

    username = config.get('username')
    password = config.get('password')

    if not username or not password:
        logging.error("設定ファイルにユーザー名またはパスワードが指定されていません。")
        return

    with sync_playwright() as p:
        browser = None
        try:
            # ヘッドレスモードに戻す
            logging.info("ブラウザを起動しています (ヘッドレスモード)...")
            browser = p.chromium.launch(headless=False)
            page = browser.new_page()

            # Bing RewardsのURLにアクセス
            logging.info(f"URLにアクセス中: https://rewards.bing.com/?form=ML2W7F")
            page.goto("https://rewards.bing.com/?form=ML2W7F")
            page.wait_for_load_state('networkidle') # ネットワークがアイドル状態になるまで待機
            page.wait_for_timeout(1000) # 1秒待機

            # ログイン処理
            # ログインページにリダイレクトされたかを確認し、ログインを実行
            if "login.live.com" in page.url:
                logging.info("ログインページにリダイレクトされました。ログイン処理を実行します。")
                # メールアドレス入力
                logging.info("メールアドレス入力フィールドを待機中...")
                page.wait_for_selector('input[type="email"]')
                page.fill('input[type="email"]', username)
                logging.info("「次へ」ボタンを待機中...")
                # 提供されたHTML要素に基づいてセレクタを修正
                page.wait_for_selector('button[type="submit"]')
                page.click('button[type="submit"]')
                page.wait_for_load_state('networkidle')
                page.wait_for_timeout(1000) # 1秒待機

                # パスワード入力
                logging.info("パスワード入力フィールドを待機中...")
                page.wait_for_selector('input[type="password"]')
                page.fill('input[type="password"]', password)
                logging.info("「次へ」ボタンを待機中...")
                # 提供されたHTML要素に基づいてセレクタを修正
                page.wait_for_selector('button[type="submit"]')
                page.click('button[type="submit"]')
                page.wait_for_load_state('domcontentloaded') # DOMContentLoadedまで待機
                page.wait_for_timeout(2000) # 2秒待機を増やす

                # 2段階認証の処理を試みる
                is_two_factor_auth_page = False
                email_send_option = None
                
                # 「本人確認」の表示有無で判別
                # strict mode violationを避けるため、page.content()でページ全体を検索
                page_content = page.content()
                if "本人確認" in page_content:
                    logging.info("「本人確認」のテキストを検出しました。2段階認証画面と判断します。")
                    is_two_factor_auth_page = True
                
                # 既存の「にメールを送信する」または「コードを送信する」ボタンを探すロジック
                # 「本人確認」が検出されなかった場合、またはメール送信オプションも確認したい場合
                if page.locator(f'span[role="button"]:has-text("にメールを送信する")').is_visible(timeout=5000):
                    email_send_option = page.locator(f'span[role="button"]:has-text("にメールを送信する")')
                    logging.info("「にメールを送信する」オプションを検出しました。")
                    is_two_factor_auth_page = True # 既存のオプションが見つかった場合も2段階認証画面と判断
                elif page.locator(f'span[role="button"]:has-text("コードを送信する")').is_visible(timeout=5000):
                    email_send_option = page.locator(f'span[role="button"]:has-text("コードを送信する")')
                    logging.info("「コードを送信する」オプションを検出しました。")
                    is_two_factor_auth_page = True # 既存のオプションが見つかった場合も2段階認証画面と判断

                if is_two_factor_auth_page:
                    logging.info("2段階認証画面を検出しました。処理を続行します。")
                    try:
                        if email_send_option: # 「本人確認」で検出した場合でも、メール送信オプションがあればクリックする
                            logging.info(f"認証コード送信オプションをクリックします。")
                            email_send_option.click()
                            page.wait_for_load_state('networkidle')
                            page.wait_for_timeout(2000) # メール送信処理の待機

                            # Gmail APIの認証
                            gmail_service = authenticate_gmail()

                            # Gmailから認証コードを取得
                            verification_code = get_verification_code_from_gmail(gmail_service, username)

                            if verification_code:
                                logging.info(f"認証コード '{verification_code}' を入力します。")
                                # 6桁のコードを1桁ずつ入力フィールドに埋める
                                for i, digit in enumerate(verification_code):
                                    field_id = f'codeEntry-{i}'
                                    logging.info(f"フィールド '{field_id}' に '{digit}' を入力中...")
                                    page.wait_for_selector(f'input[id="{field_id}"]')
                                    page.fill(f'input[id="{field_id}"]', digit)
                                    page.wait_for_timeout(200) # 各桁入力間の短い待機

                                logging.info("認証コードの入力が完了しました。自動的に認証処理に進むことを期待します。")
                                page.wait_for_load_state('networkidle', timeout=30000) # 認証処理の完了を待機
                                page.wait_for_timeout(2000) # 2秒待機

                            else:
                                logging.error("認証コードを取得できませんでした。2段階認証をスキップします。")
                        else:
                            logging.warning("「本人確認」は検出されましたが、メール送信オプションが見つかりませんでした。手動での対応が必要かもしれません。")
                    except Exception as e:
                        logging.error(f"2段階認証処理中にエラーが発生しました: {e}", exc_info=True)
                        # エラーが発生しても処理を続行する方針で

                # 「サインインの状態を維持しますか？」の処理（もし表示された場合）
                try:
                    # 「はい」ボタンのCSSセレクタは 'input[id="idSIButton9"]' であることが多いです。
                    logging.info("「サインインの状態を維持しますか？」の「はい」ボタンを待機中...")
                    keep_signed_in_button = page.locator('input[id="idSIButton9"]')
                    if keep_signed_in_button.is_visible():
                        logging.info("「サインインの状態を維持しますか？」の「はい」ボタンをクリックします。")
                        keep_signed_in_button.click()
                        page.wait_for_load_state('networkidle')
                        page.wait_for_timeout(1000) # 1秒待機
                except Exception as e:
                    logging.info(f"「サインインの状態を維持しますか？」の「はい」ボタンは表示されませんでした、またはクリックに失敗しました: {e}")

                logging.info("ログイン処理が完了しました。")
                # ログイン後、再度Rewardsページにリダイレクトされることを期待
                page.goto("https://rewards.bing.com/?form=ML2W7F")
                page.wait_for_load_state('networkidle')
                page.wait_for_timeout(1000) # 1秒待機

            # 日々のセットの項目をクリック
            logging.info("日々のセットの項目を検索しています...")
            # ここに日々のセットの項目を特定するCSSセレクタを記述します。
            # 例: .daily-set-item, .quiz-card, .poll-card など
            # 実際のウェブサイトのHTML構造に合わせて調整してください。
            # 実際のウェブサイトのHTML構造に合わせて調整してください。
            # スクリーンショットとソースコードから、各項目は 'mee-rewards-daily-set-item-content' 要素であると推測されます。
            daily_set_items = page.locator('mee-rewards-daily-set-item-content')

            if daily_set_items.count() == 0:
                logging.warning("日々のセットの項目が見つかりませんでした。セレクタを確認してください。")
            else:
                logging.info(f"{daily_set_items.count()} 個の項目が見つかりました。クリックを開始します。")
                for i in range(daily_set_items.count()):
                    try:
                        item_element = daily_set_items.nth(i)
                        # mee-rewards-daily-set-item-content 内の 'ds-card-sec' クラスを持つ要素をクリック
                        clickable_link = item_element.locator('.ds-card-sec').first
                        
                        if clickable_link.is_visible():
                            logging.info(f"項目 {i+1} をクリック中...")
                            clickable_link.click()
                            page.wait_for_load_state('networkidle', timeout=10000) # クリック後のページ遷移を待機
                            page.wait_for_timeout(1000) # 1秒待機

                            # クリック成功判定（アイコンの変化を監視）
                            try:
                                logging.info(f"項目 {i+1} のクリック成功を示すアイコンの変化を待機中...")
                                # クリックされた要素内の '+' アイコンを探す
                                plus_icon_locator = item_element.locator('.mee-icon-AddMedium')
                                # 次に、クリックされたカード内のチェックマークアイコンを探す
                                check_icon_locator = item_element.locator('.mee-icon-SkypeCircleCheck')

                                # '+' アイコンが非表示になるのを待機
                                expect(plus_icon_locator).to_be_hidden(timeout=10000) # 10秒待機
                                # チェックマークアイコンが表示されるのを待機
                                expect(check_icon_locator).to_be_visible(timeout=10000) # 10秒待機
                                
                                logging.info(f"項目 {i+1} のアイコンがチェックマークに変化しました。クリック成功と判断します。")
                            except Exception as icon_e:
                                logging.warning(f"項目 {i+1} のアイコン変化を検出できませんでした: {icon_e}")
                            
                            # 元のページに戻り、次の項目を処理するために待機
                            page.wait_for_timeout(2000) # 2秒待機
                        else:
                            logging.warning(f"項目 {i+1} のクリック可能な要素が見つからないか、表示されていません。")

                    except Exception as e:
                        logging.error(f"項目 {i+1} のクリック中にエラーが発生しました。", exc_info=True)
                        # エラーが発生しても次の項目に進む
                        continue

            logging.info("すべての項目のクリックを試行しました。")

        except Exception as e:
            logging.error(f"予期せぬエラーが発生しました。", exc_info=True)
        finally:
            if browser:
                logging.info("ブラウザを閉じています...")
                browser.close()
            logging.info("処理が終了しました。")

if __name__ == "__main__":
    run_bot()
