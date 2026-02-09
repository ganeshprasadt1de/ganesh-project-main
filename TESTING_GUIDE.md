# Quick Testing Guide

## Prerequisites
```bash
pip install pygame
```

## Basic 3-Server Setup

### Terminal 1 - Server 1
```bash
python server.py
```

### Terminal 2 - Server 2
```bash
python server.py
```

### Terminal 3 - Server 3
```bash
python server.py
```

**Expected Output:**
- Servers discover each other via UDP broadcast
- Highest UUID elected as leader
- Leader sends heartbeats every 1 second
- All servers print membership table

## Client Testing

### Terminal 4 - Player 1
```bash
python client.py --player 1
```
**Expected:** Console shows "Waiting for both players to join..." - NO pygame window yet

### Terminal 5 - Player 2
```bash
python client.py --player 2
```
**Expected:** Both pygame windows open simultaneously and game starts

## Failover Testing

### Test 1: Kill Follower
1. Identify a follower from server logs
2. Ctrl+C that server
3. **Expected:** Other servers detect death after ~10s, prune from membership, game continues

### Test 2: Kill Leader (3-server → 2-server)
1. Identify leader from logs
2. Ctrl+C the leader
3. **Expected:** 
   - Remaining 2 servers detect timeout after 5-6s
   - "2-server mode: Auto-electing higher peer..." appears
   - No election storm
   - Game continues with new leader

### Test 3: Kill Second Server (2-server → 1-server)
1. Kill one of the remaining two servers
2. **Expected:**
   - Remaining server becomes solo leader
   - Game may pause (not enough players)

## What Should NOT Happen

❌ No "Leader timeout! Starting election." spam when leader is healthy
❌ No multiple "I AM LEADER NOW" messages
❌ No pygame windows opening before both players join
❌ No "Connection lost retrying" spam in client console
❌ No permanent freeze with no leader

## Verification Checklist

- [ ] 3 servers start and elect leader
- [ ] Membership table shows correct peers
- [ ] Clients wait for both players before opening windows
- [ ] Game runs smoothly with all players
- [ ] Leader failover completes in 5-6 seconds
- [ ] 2-server mode auto-elects without election storm
- [ ] No false elections with healthy leader
- [ ] No double leader messages
- [ ] Client reconnects silently on server restart

## Debugging Tips

1. **Check Leader:** Look for "*** I AM LEADER NOW ***" in server logs
2. **Check Heartbeats:** Leader should log heartbeat sends every 1s
3. **Check Membership:** Servers print membership table on elections
4. **Check Client State:** Clients log when both players are ready
5. **Monitor Timeouts:** Should not see "Leader timeout" unless leader actually dies

## Expected Timing

- Heartbeat interval: 1 second
- Heartbeat timeout: 5-6 seconds (5s + 0-1s jitter)
- Election cooldown: 3 seconds
- Client timeout: 6 seconds
- Peer pruning: 10 seconds (2x heartbeat timeout)

## Performance Notes

- Network traffic reduced 50% (only leader sends heartbeats)
- Elections rarely happen with healthy leader
- 2-server failover is instant (no election needed)
- Client UI is responsive with no premature windows
