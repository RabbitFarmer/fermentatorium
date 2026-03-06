# Temperature Controller Display Tracing Guide

## Overview

This guide explains how to use the tracing system added to diagnose temperature controller display issues.

## Where to Find Traces

### 1. Server-Side Traces (Backend)

**How to view**:
```bash
python3 app3.py
```

**What to look for**: Lines starting with `[TRACE]`

#### Startup Traces
```
[TRACE] Loaded temp config from config/temp_control_config.json
[TRACE] temp_cfg_raw has 'controllers' key: True/False
[TRACE] Number of controllers in config: X
[TRACE] Config already in new format with X controllers
```

#### Dashboard Page Load Traces
```
[TRACE] dashboard() route called
[TRACE] Number of controllers being passed to template: X
[TRACE] Number of active tilts: X
```

#### Temp Config Page Load Traces
```
[TRACE] temp_config() route called
[TRACE] Number of controllers in temp_cfg: X
[TRACE] Using existing controller 0
```

### 2. Browser Console Traces (Frontend)

Press `F12` to open Developer Tools, click Console tab.

#### Main Dashboard Console Traces
```
[TRACE] maindisplay.html loaded
[TRACE] Controllers data: [{...}, {...}, {...}]
[TRACE] Number of controllers: 3
```

#### Temp Config Page Console Traces
```
[TRACE] temp_control_config.html loaded
[TRACE] Number of controllers: 3
```

### 3. Visual Debug Boxes

Both pages have yellow debug information boxes at the top.

## Common Issues

### Issue 1: Config file in old single-controller format
**Traces will show**:
```
[TRACE] temp_cfg_raw has 'controllers' key: False
[MIGRATION] Migrating old single-controller config to 3-controller format
```

### Issue 2: Config file missing or corrupted
**Traces will show**:
```
[TRACE] Number of controllers in config: 0
[TRACE] Added 3 new controllers, saving config
```

### Issue 3: Controllers array not being passed to template
**Traces will show**:
```
Server: [TRACE] Number of controllers being passed to template: 3
Browser: [TRACE] Number of controllers: 0
```

## Config File Structure

The `config/temp_control_config.json` should look like this:

```json
{
  "controllers": [
    {
      "controller_id": 0,
      "tilt_color": "Red",
      ...
    },
    {
      "controller_id": 1,
      ...
    },
    {
      "controller_id": 2,
      ...
    }
  ]
}
```

## Production Deployment Considerations

**IMPORTANT**: Remove or disable traces before production deployment:
1. Remove `[TRACE]` print statements from `app3.py`
2. Remove console.log traces from templates
3. Remove yellow debug boxes from templates
