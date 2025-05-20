from telegram.ext import (
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    filters,
)

WELCOME_GROUP_ID=-1002349894738

(
    MAIN_MENU,
    INPUT_CONTACTS,
    INPUT_MESSAGE,
    INPUT_LINKS,
    INPUT_FORWARD_LIMIT,
    INPUT_SELECTED_CONTACTS,
    SETTINGS_MENU,
    MANAGE_CONTACTS,
    BROADCAST_CONFIRM,
    EXTERNAL_INPUT_CONTACTS,
    EXTERNAL_INPUT_MESSAGE,
    EXTERNAL_INPUT_LINKS,
    EXTERNAL_INPUT_LIMIT,
    EXTERNAL_BROADCAST_CONFIRM,
    EXTERNAL_INPUT_FORWARD_LIMIT,
    INTERNAL_BROADCAST_CONFIRM,
    DELETE_CONTACT
) = range(17)


def setup_handlers(self):
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", self.start)],
        states={
            MAIN_MENU: [
                CallbackQueryHandler(self.show_settings, pattern="^view_settings$"),
                CallbackQueryHandler(self.settings_menu, pattern="^settings$"),
                CallbackQueryHandler(self.external_switch_broadcast_mode_handler,
                                     pattern="^external_switch_broadcast_mode$"),
                CallbackQueryHandler(self.external_edit_text_handler, pattern="^external_edit_text$"),
                CallbackQueryHandler(self.external_edit_links_handler, pattern="^external_edit_links$"),
                CallbackQueryHandler(self.main_menu, pattern="^main_menu$"),
                CallbackQueryHandler(self.show_internal_menu, pattern="^internal_menu$"),
                CallbackQueryHandler(self.internal_check_config_handler, pattern="^internal_check_config$"),
                CallbackQueryHandler(self.internal_broadcast_handler, pattern="^internal_broadcast$"),
                CallbackQueryHandler(self.internal_broadcast_selected_handler, pattern="^internal_broadcast_selected$"),
                CallbackQueryHandler(self.internal_broadcast_confirm_handler, pattern="^internal_broadcast_confirm$"),
                CallbackQueryHandler(self.internal_broadcast_handler, pattern="^internal_broadcast_go$"),
                CallbackQueryHandler(self.add_contacts_handler, pattern="^add_contacts$"),
                CallbackQueryHandler(self.external_settings_handler, pattern="^external_settings$"),
                CallbackQueryHandler(self.external_set_contacts_handler, pattern="^external_set_contacts$"),
                CallbackQueryHandler(self.external_broadcast_handler, pattern="^external_broadcast$"),
                CallbackQueryHandler(self.external_broadcast_confirm_handler, pattern="^external_broadcast_confirm$"),
                CallbackQueryHandler(self.external_edit_limit_handler, pattern="^external_edit_limit$"),
                CallbackQueryHandler(self.external_set_forward_limit_handler, pattern="^external_set_forward_limit$"),
                CallbackQueryHandler(self.set_internal_forward_limit_handler, pattern="^set_internal_forward_limit$")
            ],
            SETTINGS_MENU: [
                CallbackQueryHandler(self.edit_text_handler, pattern="^edit_text$"),
                CallbackQueryHandler(self.main_menu, pattern="^main_menu$"),
                CallbackQueryHandler(self.switch_broadcast_mode_handler, pattern="^switch_broadcast_mode$"),
                CallbackQueryHandler(self.edit_links_handler, pattern="^edit_links$"),
                CallbackQueryHandler(self.manage_contacts_handler, pattern="^manage_contacts$"),
                CallbackQueryHandler(self.show_internal_menu, pattern="^internal_menu$"),
                CallbackQueryHandler(self.set_internal_forward_limit_handler, pattern="^set_internal_forward_limit$"),

            ],
            INPUT_CONTACTS: [
                MessageHandler(filters.TEXT | filters.Document.TEXT, self.process_contacts),
                CallbackQueryHandler(self.settings_menu, pattern="^cancel$")
            ],
            INPUT_MESSAGE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, self.process_message_text),
                CallbackQueryHandler(self.settings_menu, pattern="^cancel$")
            ],
            MANAGE_CONTACTS: [
                CallbackQueryHandler(self.add_contacts_handler, pattern="^add_contacts$"),
                CallbackQueryHandler(self.to_settings_menu_handler, pattern="^to_settings_menu$"),
                CallbackQueryHandler(self.load_selected_contacts_handler, pattern="^load_selected_contacts$"),
                CallbackQueryHandler(self.delete_contact_handler, pattern="^delete_contact$")
            ],
            BROADCAST_CONFIRM: [
                CallbackQueryHandler(self.confirm_broadcast, pattern="^confirm_"),
                CallbackQueryHandler(self.main_menu, pattern="^main_menu$")
            ],
            EXTERNAL_INPUT_CONTACTS: [
                MessageHandler(filters.TEXT | filters.Document.TEXT, self.process_external_contacts),
                CallbackQueryHandler(self.main_menu, pattern="^cancel$"),
            ],
            EXTERNAL_INPUT_MESSAGE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, self.process_external_message_text),
                CallbackQueryHandler(self.external_settings_handler, pattern="^cancel$")
            ],
            EXTERNAL_BROADCAST_CONFIRM: [
                CallbackQueryHandler(self.external_broadcast_handler, pattern="^external_broadcast$"),
                CallbackQueryHandler(self.main_menu, pattern="^main_menu$")
            ],
            INTERNAL_BROADCAST_CONFIRM: [
                CallbackQueryHandler(self.internal_broadcast_handler, pattern="^internal_broadcast_go$"),
                CallbackQueryHandler(self.show_internal_menu, pattern="^internal_menu$"),
            ],
            DELETE_CONTACT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, self.process_delete_contact),
                CallbackQueryHandler(self.manage_contacts_handler, pattern="^cancel$")
            ],
            EXTERNAL_INPUT_LIMIT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, self.process_external_limit),
                CallbackQueryHandler(self.external_settings_handler, pattern="^cancel$")
            ],
            INPUT_LINKS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, self.process_links),
                CallbackQueryHandler(self.settings_menu, pattern="^cancel$")
            ],
            EXTERNAL_INPUT_FORWARD_LIMIT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, self.process_external_forward_limit),
                CallbackQueryHandler(self.external_settings_handler, pattern="^cancel$")
            ],
            INPUT_FORWARD_LIMIT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, self.process_internal_forward_limit),
                CallbackQueryHandler(self.settings_menu, pattern="^cancel$")
            ],
            INPUT_SELECTED_CONTACTS: [
                MessageHandler(filters.Document.ALL, self.process_selected_contacts),
                MessageHandler(filters.TEXT & filters.Regex("^❌ Отмена$"), self.cancel_handler),
                CallbackQueryHandler(self.cancel_handler, pattern="^cancel$"),
            ]
        },
        fallbacks=[
            CommandHandler("cancel", self.cancel_handler),
            CallbackQueryHandler(self.main_menu, pattern="^main_menu$"),
            CallbackQueryHandler(self.cancel_broadcast, pattern="^cancel_broadcast$")
        ],
        allow_reentry=True
    )
    self.ptb_app.add_handler(conv_handler)
    self.ptb_app.add_error_handler(self.error_handler)
    self.ptb_app.add_handler(
        MessageHandler(filters.Chat(WELCOME_GROUP_ID) & filters.StatusUpdate.NEW_CHAT_MEMBERS, self.on_new_member))