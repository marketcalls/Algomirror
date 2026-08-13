from flask import render_template, redirect, url_for, flash, request, current_app
from flask_login import login_required, current_user
from app.admin import admin_bp
from app.models import AppSettings, ActivityLog
from app import db
from app.utils.rate_limiter import auth_rate_limit


def log_activity(action, details=None):
    """Helper function to log admin activities"""
    try:
        log_entry = ActivityLog(
            user_id=current_user.id,
            action=action,
            details=details,
            ip_address=request.remote_addr,
            user_agent=request.headers.get('User-Agent'),
            status='success'
        )
        db.session.add(log_entry)
        db.session.commit()
    except Exception as e:
        current_app.logger.error(f'Failed to log activity: {str(e)}')


@admin_bp.route('/settings')
@login_required
@auth_rate_limit()
def settings():
    """Platform settings page (admin only) - feature toggles for the strategy engine"""
    if not current_user.is_admin:
        flash('Access denied. Admin privileges required.', 'error')
        return redirect(url_for('main.dashboard'))

    app_settings = AppSettings.get()
    return render_template('admin/settings.html', app_settings=app_settings)


@admin_bp.route('/settings/update', methods=['POST'])
@login_required
@auth_rate_limit()
def update_settings():
    """Toggle platform-wide feature flags (admin only)"""
    if not current_user.is_admin:
        flash('Access denied. Admin privileges required.', 'error')
        return redirect(url_for('main.dashboard'))

    app_settings = AppSettings.get()
    was_enabled = app_settings.strategy_engine_enabled
    app_settings.strategy_engine_enabled = 'strategy_engine_enabled' in request.form
    db.session.commit()

    if was_enabled != app_settings.strategy_engine_enabled:
        state = 'enabled' if app_settings.strategy_engine_enabled else 'disabled'
        log_activity('platform_settings_update', f'Strategy engine {state}')
        # Nav/dashboard/reconciliation changes apply immediately (read the flag
        # fresh on each request). The background services (Risk Manager,
        # Supertrend Exit, Order Poller) are only started/stopped at app
        # startup, so restart the service for that part to take effect.
        flash(f'Strategy engine {state}. Restart the AlgoMirror service for background-service changes to take effect.', 'success')

    return redirect(url_for('admin.settings'))
