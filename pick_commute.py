#!/usr/local/bin/python
import argparse
import datetime
import json
import re
import time
import urllib
import urllib2

from xml.etree import ElementTree

DIRECTIONS_URL = 'https://maps.googleapis.com/maps/api/directions/json'

# http://www.nextbus.com/xmlFeedDocs/NextBusXMLFeed.pdf
NEXTBUS_URL = 'http://webservices.nextbus.com/service/publicXMLFeed'
AGENCY = 'sf-muni'


def http_get(url_base, params):
    response = urllib2.urlopen("%s?%s" % (url_base, urllib.urlencode(params)))
    return response.read()

non_alphanum = re.compile('[\W_]+')


def normalize(route_tag):
    return non_alphanum.sub('', route_tag)


def find_stop(route_config, location):
    """Find the stop on the given NextBus route with the minimum
    distance to the given Google Maps location"""
    latitude = location['lat']
    longitude = location['lng']

    def distance(stop):
        return ((float(stop.attrib['lat']) - latitude)**2 +
                (float(stop.attrib['lon']) - longitude)**2)

    return min(route_config.findall('stop'), key=distance)


def get_direction_name(
        route_config, direction_tag, departure_tag, arrival_tag):
    for d in route_config.findall('direction'):
        if d.attrib.get('tag') == direction_tag:
            found_departure = False
            for s in d:
                stop_tag = s.attrib.get('tag')
                if stop_tag == departure_tag:
                    found_departure = True
                elif found_departure and stop_tag == arrival_tag:
                    return d.attrib.get('name')
            break
    return None


def transit_departure(transit_details, route_to_config, current_time):
    # Get the route from Google
    google_route_name = transit_details['line']['short_name']
    # Normalize route_tag
    route_config = route_to_config[normalize(google_route_name)]
    # Get NextBus route
    route_tag = route_config.attrib.get('tag')

    # Find the NextBus stops at the given lat/lon
    departure_loc = transit_details['departure_stop']['location']
    departure_tag = find_stop(route_config, departure_loc).attrib.get('tag')

    arrival_loc = transit_details['arrival_stop']['location']
    arrival_tag = find_stop(route_config, arrival_loc).attrib.get('tag')

    print('\t\t%s from %s to %s' % (
            transit_details['line']['short_name'],
            transit_details['departure_stop']['name'],
            transit_details['arrival_stop']['name']))

    # Get the predicted departure times for the route & stop
    predictions = ElementTree.fromstring(http_get(NEXTBUS_URL, {
                'command': 'predictions',
                'a': AGENCY,
                'r': route_tag,
                's': departure_tag,
    }))

    direction = None
    for d in predictions[0]:
        for p in d:
            # If we won't miss the predicted departure and the arrival
            # location is in this direction, return the prediction
            prediction_dir = get_direction_name(
                route_config, p.attrib.get('dirTag'),
                departure_tag, arrival_tag)
            if prediction_dir is None:
                continue
            direction = prediction_dir
            timestamp = int(p.attrib.get('epochTime')) / 1000
            prediction_time = datetime.datetime.fromtimestamp(timestamp)
            if prediction_time >= current_time:
                print("\t\tDepart at %s" % prediction_time.time())
                return prediction_time

    # No prediction.  Find the next scheduled time
    today = datetime.date.today()

    # Only look at relevant schedules
    valid_schedule = {5: 'sat', 6: 'sun'}.get(today.weekday(), 'wkd')

    next_departure = datetime.datetime.max  # The next scheduled departure

    schedule_params = {'command': 'schedule', 'a': AGENCY, 'r': route_tag}
    for r in ElementTree.fromstring(http_get(NEXTBUS_URL, schedule_params)):
        if (r.attrib.get('serviceClass') != valid_schedule or
                r.attrib.get('direction') != direction):
            continue

        for b in r.findall('tr'):
            for s in b:
                if s.attrib.get('tag') != departure_tag:
                    continue
                # epochTime is the number of milliseconds from midnight
                timestamp = int(s.attrib.get('epochTime')) / 1000

                # Get that time for today
                scheduled_time = datetime.datetime.combine(
                    today, datetime.datetime.fromtimestamp(timestamp).time())

                # If it's already passed, get the time for tomorrow.
                # TODO: This is a hack; handle if today is Friday and
                # tomorrow has a different schedule for Saturday.
                while scheduled_time < current_time:
                    scheduled_time += datetime.timedelta(1)

                if scheduled_time < next_departure:
                    next_departure = scheduled_time
                break
    if next_departure < datetime.datetime.max:
        print("\t\tDepart at %s (scheduled)" % next_departure.time())
    else:
        print("\t\tERROR: Unable to find next scheduled departure")
    return next_departure

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('origin')
    parser.add_argument('destination')

    args = parser.parse_args()

    # Get config info from NextBus
    route_to_config = {}
    for r in ElementTree.fromstring(
        http_get(NEXTBUS_URL, {'command': 'routeConfig', 'a': AGENCY})):
        if r.tag == 'route':
            route_to_config[normalize(r.attrib.get('tag'))] = r

    # Get routes from Google
    directions_params = {
        'origin': args.origin,
        'destination': args.destination,
        'sensor': 'false',
        'mode': 'transit',
        'departure_time': int(time.time()),
        'alternatives': 'true',
    }
    routes = json.loads(http_get(DIRECTIONS_URL, directions_params))['routes']

    best_arrival = datetime.datetime.max
    best_route = None
    for r_i, r in enumerate(routes):
        current_time = datetime.datetime.now()

        print("Route %d" % (r_i + 1))

        # For each step:
        for s_i, s in enumerate(r['legs'][0]['steps']):
            print("\t%d. %s" % (s_i + 1, s['html_instructions']))
            # If it is transit:
            if s['travel_mode'] == 'TRANSIT':
                current_time = transit_departure(
                    s['transit_details'], route_to_config, current_time)
                if current_time == datetime.datetime.max:
                    break

            # Add the step's duration to the current time
            current_time += datetime.timedelta(seconds=s['duration']['value'])

            print("\t\tArrive at %s" % current_time.time())

        if current_time < best_arrival:
            best_arrival = current_time
            best_route = r_i + 1

    # Output the best one
    print("Take route %d, arriving at %s" %
          (best_route, best_arrival))

if __name__ == '__main__':
    main()
