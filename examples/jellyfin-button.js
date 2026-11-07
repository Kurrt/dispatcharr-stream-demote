/*
 * Flag-as-bad button for the Jellyfin web player. An example to copy and adapt.
 *
 * Adds a button to the player controls while live TV is playing. Pressing it tells
 * Dispatcharr the channel looks wrong. Dispatcharr works out which stream is playing,
 * moves it down the channel's order and switches to the next one.
 *
 * Read "Using the button safely" in the README first. Never put a token in this file:
 * scripts injected into Jellyfin are usually served to anyone who can load the page.
 *
 * Written for older TV browsers: nothing newer than ES2018.
 */
(function () {
  'use strict';

  var CONFIG = {
    // Direct mode: your Dispatcharr's address. It must be https unless it's on this machine.
    // Gateway mode: a path on your Jellyfin address, such as '/flag-stream'.
    flagUrl: 'https://dispatcharr.example.com/api/stream-demote/flag/',
    // True if a gateway beside Jellyfin checks the sign-in and holds the flag token.
    useGateway: false,
    // Where this device keeps its flag token in direct mode.
    storageKey: 'streamDemote.flagToken',
    buttonClass: 'btnFlagStream',
    // The player's own control row in Jellyfin Web.
    controlsSelector: '#videoOsdPage .videoOsdBottom .buttons > div'
  };

  var MESSAGES = {
    rotated: 'Thanks. Switching to the next stream.',
    demoted: 'Thanks. That stream has been moved down.',
    observed: 'Noted. The server is in dry run, so nothing changed.',
    noted: 'Noted. There is no other stream to try.',
    not_playing: 'This channel is not playing through Dispatcharr.',
    forbidden: 'Dispatcharr does not take reports from this network.',
    ambiguous: 'More than one channel has this number, so nothing changed.',
    rate_limited: 'Already reported. Try again later.',
    paused: 'Reports are paused for now.',
    channel_problem: 'Every stream has been reported, so this may be the programme itself.',
    unauthorized: 'The flag token was refused. It has been forgotten on this device.',
    bad_request: 'That request could not be understood.'
  };

  function readToken() {
    try {
      return window.localStorage.getItem(CONFIG.storageKey);
    } catch (error) {
      return null;
    }
  }

  function saveToken(token) {
    try {
      window.localStorage.setItem(CONFIG.storageKey, token);
    } catch (error) {
      // Private windows can refuse. The token then lasts for this one press.
    }
  }

  function forgetToken() {
    try {
      window.localStorage.removeItem(CONFIG.storageKey);
    } catch (error) {
      // Nothing was stored, so there is nothing to forget.
    }
  }

  function isSafeAddress(url) {
    var parsed = new URL(url, window.location.href);
    if (CONFIG.useGateway) {
      return parsed.origin === window.location.origin;
    }
    return parsed.protocol === 'https:' || parsed.hostname === 'localhost' || parsed.hostname === '127.0.0.1';
  }

  function authorizationHeader(api) {
    if (CONFIG.useGateway) {
      // Only ever sent to this page's own address. See isSafeAddress.
      return 'MediaBrowser Token="' + api.accessToken() + '"';
    }
    var token = readToken();
    if (!token) {
      token = window.prompt('Enter this device\'s flag token. It can only report a bad stream.');
      if (!token) {
        return null;
      }
      token = token.trim();
      saveToken(token);
    }
    return 'Bearer ' + token;
  }

  // Resolves to the channel number playing on this device, or null if it isn't live TV.
  function currentChannelNumber(api) {
    return api.getSessions({ DeviceId: api.deviceId() }).then(function (sessions) {
      var session = sessions && sessions[0];
      var item = session && session.NowPlayingItem;
      if (!item || item.Type !== 'TvChannel') {
        return null;
      }
      if (item.ChannelNumber) {
        return String(item.ChannelNumber);
      }
      return api.getLiveTvChannel(item.Id, api.getCurrentUserId()).then(function (channel) {
        return channel && channel.ChannelNumber ? String(channel.ChannelNumber) : null;
      });
    });
  }

  function send(channelNumber, authorization) {
    return window.fetch(CONFIG.flagUrl, {
      method: 'POST',
      // No cookies, and nothing about this page in the Referer.
      credentials: 'omit',
      referrerPolicy: 'no-referrer',
      headers: { 'Content-Type': 'application/json', Authorization: authorization },
      body: JSON.stringify({ channel_number: channelNumber })
    }).then(function (response) {
      return response.json().catch(function () {
        return {};
      }).then(function (data) {
        return { status: response.status, result: data && data.result, retryAfter: response.headers && response.headers.get('Retry-After') };
      });
    });
  }

  function flag() {
    var api = window.ApiClient;
    if (!api) {
      return Promise.resolve('Not signed in to Jellyfin.');
    }
    if (!isSafeAddress(CONFIG.flagUrl)) {
      return Promise.resolve('The flag address is not safe to send a token to. Check flagUrl.');
    }
    return currentChannelNumber(api).then(function (number) {
      if (!number) {
        return 'This only works on live TV.';
      }
      var authorization = authorizationHeader(api);
      if (!authorization) {
        return 'No token entered, so nothing was sent.';
      }
      return send(number, authorization).then(function (reply) {
        if (reply.status === 401 && !CONFIG.useGateway) {
          forgetToken();
        }
        if (reply.result === 'rate_limited' && reply.retryAfter) {
          // Up to an hour, if this was the limit on the token and not on the channel.
          var minutes = Math.ceil(Number(reply.retryAfter) / 60);
          return 'Already reported. Try again in ' + minutes + (minutes === 1 ? ' minute.' : ' minutes.');
        }
        return MESSAGES[reply.result] || 'The report did not go through.';
      });
    }).catch(function () {
      return 'The report did not go through.';
    });
  }

  function showToast(text) {
    var toast = document.createElement('div');
    toast.textContent = text;
    toast.setAttribute('role', 'status');
    toast.style.cssText = 'position:fixed;left:50%;bottom:6em;transform:translateX(-50%);z-index:9999;' +
      'padding:.6em 1em;border-radius:.3em;background:rgba(0,0,0,.85);color:#fff;font-size:1.1em;max-width:90vw;';
    document.body.appendChild(toast);
    window.setTimeout(function () {
      if (toast.parentNode) {
        toast.parentNode.removeChild(toast);
      }
    }, 5000);
  }

  function addButton(row) {
    var button = document.createElement('button');
    button.type = 'button';
    button.className = 'paper-icon-button-light autoSize ' + CONFIG.buttonClass;
    button.title = 'Report a bad stream';
    button.setAttribute('aria-label', 'Report a bad stream');
    var icon = document.createElement('span');
    icon.className = 'xlargePaperIconButton material-icons report_problem';
    icon.setAttribute('aria-hidden', 'true');
    button.appendChild(icon);
    button.addEventListener('click', function () {
      button.disabled = true;
      flag().then(function (text) {
        button.disabled = false;
        showToast(text);
      });
    });
    row.insertBefore(button, row.firstChild);
    return button;
  }

  // Show the button only while live TV plays. Other videos never see it.
  function refreshButton(row) {
    var api = window.ApiClient;
    if (!api) {
      return;
    }
    lastRefresh = Date.now();
    var button = row.querySelector('.' + CONFIG.buttonClass);
    if (!button) {
      button = addButton(row);
      button.style.display = 'none';
    }
    currentChannelNumber(api).then(function (number) {
      button.style.display = number ? '' : 'none';
    }).catch(function () {
      button.style.display = 'none';
    });
  }

  var waiting = false;
  var lastRefresh = 0;
  function onPageChange() {
    if (waiting) {
      return;
    }
    waiting = true;
    window.setTimeout(function () {
      waiting = false;
      var row = document.querySelector(CONFIG.controlsSelector);
      // The controls can stay on the page from one video to the next, so look again now and then.
      if (row && (!row.querySelector('.' + CONFIG.buttonClass) || Date.now() - lastRefresh > 30000)) {
        refreshButton(row);
      }
    }, 500);
  }

  // Lets you sign a device out with: StreamDemoteFlag.forgetToken()
  window.StreamDemoteFlag = { flag: flag, forgetToken: forgetToken };

  if (window.MutationObserver && document.body) {
    new window.MutationObserver(onPageChange).observe(document.body, { childList: true, subtree: true });
    onPageChange();
  }
})();
