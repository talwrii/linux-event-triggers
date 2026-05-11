# linux-event-triggers
Run certain triggers when a linux event file received an event. Prevent other processes from receiving the event.

AI-generated and unreviewed.

## Alternatives and prior work
`kbd` can remap keys for keyboards.  It also supports `command` to run commands. I already used `kbd` as a separate device and didn't want to mix up another device and activity in this.

`triggerhappy` basically does the same thing as this program. However, when running in non-daemon mode it does not seem able to grab the event (or so claude says) and I don't really want to mess around with daemons.

## Installation
`pipx install linux-event-triggers`

## Usage
Print when a is clicked, use sudo to own the device.

`evtrig $DEVICE --chown --bind "a=echo hello"`

Where $DEVICE is an event device. Note that you can use udevadm to find a *fixed* path for the event - indexed by the usb device name rather that `event*`.

`--chown` use `sudo` to take ownership of the event file. By using the fixed filename.



