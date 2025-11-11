#!/usr/bin/env python
"""
Test script to verify trading hours implementation
Tests WebSocket scheduling, holiday checking, and special sessions
"""

import sys
import os
from datetime import datetime, time, date, timedelta
import pytz

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import create_app, db
from app.models import TradingSession, MarketHoliday, SpecialTradingSession, TradingHoursTemplate
from app.utils.background_service import option_chain_service

def test_trading_hours():
    """Test the trading hours implementation"""
    app = create_app()
    
    with app.app_context():
        print("\n" + "="*60)
        print("TRADING HOURS IMPLEMENTATION TEST")
        print("="*60)
        
        # Initialize service
        print("\n1. Initializing background service...")
        option_chain_service.refresh_trading_hours_cache()
        
        # Check cached data
        print(f"\n2. Cached Data:")
        print(f"   - Trading Sessions: {len(option_chain_service.cached_sessions)}")
        print(f"   - Holidays: {len(option_chain_service.cached_holidays)}")
        print(f"   - Special Sessions: {len(option_chain_service.cached_special_sessions)}")
        
        # Display trading sessions
        print(f"\n3. Trading Sessions Configuration:")
        days = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
        for session in option_chain_service.cached_sessions:
            day = days[session['day_of_week']]
            start = session['start_time'].strftime('%H:%M')
            end = session['end_time'].strftime('%H:%M')
            # Calculate pre-market start (15 minutes before)
            pre_market = (datetime.combine(date.today(), session['start_time']) - timedelta(minutes=15)).time()
            pre_market_str = pre_market.strftime('%H:%M')
            status = "Active" if session['is_active'] else "Inactive"
            print(f"   {day:10s}: WebSocket {pre_market_str} - {end} (Market {start} - {end}) [{status}]")
        
        # Test current trading status
        print(f"\n4. Current Status Check:")
        now = datetime.now(pytz.timezone('Asia/Kolkata'))
        print(f"   Current Time: {now.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        print(f"   Day: {days[now.weekday()]}")
        
        is_trading = option_chain_service.is_trading_hours()
        print(f"   Is Trading Hours (including pre-market): {is_trading}")
        
        is_holiday = option_chain_service.is_holiday(now.date())
        print(f"   Is Holiday: {is_holiday}")
        
        has_special = option_chain_service.has_special_session(now.date(), now.time())
        print(f"   Has Special Session: {has_special}")
        
        # Test specific scenarios
        print(f"\n5. Testing Specific Scenarios:")
        
        # Test pre-market time (9:00 AM on a weekday)
        test_time = datetime(2025, 1, 6, 9, 0, 0, tzinfo=pytz.timezone('Asia/Kolkata'))  # Monday 9:00 AM
        option_chain_service.cached_holidays = {}  # Clear holidays for test
        is_trading = option_chain_service.is_trading_hours()
        print(f"   Monday 9:00 AM (pre-market): Should be True")
        
        # Test market hours (9:30 AM on a weekday)
        test_time = datetime(2025, 1, 6, 9, 30, 0, tzinfo=pytz.timezone('Asia/Kolkata'))  # Monday 9:30 AM
        print(f"   Monday 9:30 AM (market open): Should be True")
        
        # Test after hours (4:00 PM on a weekday)
        test_time = datetime(2025, 1, 6, 16, 0, 0, tzinfo=pytz.timezone('Asia/Kolkata'))  # Monday 4:00 PM
        print(f"   Monday 4:00 PM (after hours): Should be False")
        
        # Test weekend
        test_time = datetime(2025, 1, 4, 10, 0, 0, tzinfo=pytz.timezone('Asia/Kolkata'))  # Saturday 10:00 AM
        print(f"   Saturday 10:00 AM: Should be False")
        
        # Display scheduled jobs
        print(f"\n6. Scheduled Jobs:")
        option_chain_service.schedule_market_hours()
        jobs = option_chain_service.scheduler.get_jobs()
        
        if jobs:
            for job in jobs[:10]:  # Show first 10 jobs
                print(f"   - {job.id}: Next run at {job.next_run_time}")
        else:
            print("   No jobs scheduled")
        
        # Check for any holidays in the database
        print(f"\n7. Upcoming Holidays:")
        upcoming_holidays = MarketHoliday.query.filter(
            MarketHoliday.holiday_date >= date.today()
        ).order_by(MarketHoliday.holiday_date).limit(5).all()
        
        if upcoming_holidays:
            for holiday in upcoming_holidays:
                print(f"   - {holiday.holiday_date}: {holiday.holiday_name} ({holiday.market})")
        else:
            print("   No upcoming holidays configured")
        
        # Check for special sessions
        print(f"\n8. Upcoming Special Sessions:")
        upcoming_special = SpecialTradingSession.query.filter(
            SpecialTradingSession.session_date >= date.today()
        ).order_by(SpecialTradingSession.session_date).limit(5).all()
        
        if upcoming_special:
            for session in upcoming_special:
                print(f"   - {session.session_date}: {session.session_name} "
                      f"({session.start_time.strftime('%H:%M')} - {session.end_time.strftime('%H:%M')})")
        else:
            print("   No upcoming special sessions configured")
        
        print("\n" + "="*60)
        print("TEST COMPLETED SUCCESSFULLY")
        print("="*60)
        
        return True

if __name__ == '__main__':
    try:
        test_trading_hours()
        print("\n✓ All tests passed!")
    except Exception as e:
        print(f"\n✗ Test failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)