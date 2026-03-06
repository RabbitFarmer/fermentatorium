# Configuration Files

This directory contains the application configuration files.

## Important: Configuration File Management

**Your configuration files will NOT be overwritten** when you update the application via git pull or rsync.

- **Template files** (`.json.template`) are tracked in git and contain default values
- **Actual config files** (`.json`) are NOT tracked in git and contain YOUR settings
- On first run, the application automatically copies templates to create your config files
- Your settings persist across application updates

## Configuration Files

- `system_config.json` - General system settings
- `temp_control_config.json` - Temperature control settings
- `tilt_config.json` - Tilt hydrometer assignments and batch information

## Template Files

Template files provide safe defaults and are used to initialize your configuration:

- `system_config.json.template` - Template for system settings
- `temp_control_config.json.template` - Template for temperature control
- `tilt_config.json.template` - Template for Tilt configurations

**Do not edit template files directly** - they will be overwritten by git updates.

## Deployment Workflow

When updating the application:

```bash
git pull origin main
```

The application will automatically create config files from templates if they don't exist.

### tilt_config.json
Contains Tilt hydrometer assignments and batch information for each color.

**Fields per tilt:**
- `beer_name`: Name of the beer being fermented
- `batch_name`: Batch identifier
- `ferm_start_date`: Fermentation start date
- `recipe_og`: Recipe original gravity
- `recipe_fg`: Recipe final gravity
- `recipe_abv`: Recipe ABV percentage
- `actual_og`: Actual measured original gravity
- `og_confirmed`: Whether the original gravity has been confirmed
- `brewid`: Auto-generated batch ID

### temp_control_config.json
Temperature control settings for the fermentation chamber.

**Fields:**
- `low_limit`: Lower temperature limit (°F)
- `high_limit`: Upper temperature limit (°F)
- `enable_heating`: Enable heating control
- `enable_cooling`: Enable cooling control
- `tilt_color`: Which Tilt to use for temperature monitoring
- `heating_plug`: IP address for heating Kasa plug
- `cooling_plug`: IP address for cooling Kasa plug
- `compressor_delay`: Delay in minutes before restarting compressor

### system_config.json
General system settings.

**Fields:**
- `brewery_name`: Your brewery name
- `brewer_name`: Your name
- `units`: Temperature units
- `tilt_inactivity_timeout_minutes`: Time after which inactive Tilts are hidden
