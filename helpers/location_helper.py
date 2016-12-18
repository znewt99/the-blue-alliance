import json
import logging
import re
import tba_config
import urllib

from difflib import SequenceMatcher
from google.appengine.api import memcache, urlfetch
from google.appengine.ext import ndb

from models.location import Location
from models.sitevar import Sitevar
from models.team import Team


class LocationHelper(object):
    GOOGLE_API_KEY = None

    @classmethod
    def get_similarity(cls, a, b):
        """
        Returns max(similarity between two strings ignoring case,
                    similarity between two strings ignoring case and order,
                    similarity between acronym(a) & b,
                    similarity between a & acronym(b)) from 0 to 1
        where acronym() is generated by splitting along non word characters
        Ignores case and order
        """
        a = a.lower().strip()
        b = b.lower().strip()

        a_split = re.split('\W+', a)
        b_split = re.split('\W+', b)
        a_sorted = ' '.join(sorted(a_split))
        b_sorted = ' '.join(sorted(b_split))
        a_acr =  ''.join([w[0] if w else '' for w in a_split]).lower()
        b_acr =  ''.join([w[0] if w else '' for w in b_split]).lower()

        return max([
            SequenceMatcher(None, a, b).ratio(),
            SequenceMatcher(None, a_sorted, b_sorted).ratio(),
            SequenceMatcher(None, a_acr, b).ratio(),
            SequenceMatcher(None, a, b_acr).ratio()
        ])

    # @classmethod
    # def get_event_lat_lng(cls, event):
    #     """
    #     Try different combinations of venue, address, and location to
    #     get latitude and longitude for an event
    #     """
    #     lat_lng, _ = cls.get_lat_lng(event.venue_address_safe)
    #     if not lat_lng:
    #         lat_lng, _ = cls.get_lat_lng(u'{} {}'.format(event.venue, event.location))
    #     if not lat_lng:
    #         lat_lng, _ = cls.get_lat_lng(event.location)
    #     if not lat_lng:
    #         lat_lng, _ = cls.get_lat_lng(u'{} {}'.format(event.city, event.country))
    #     if not lat_lng:
    #         logging.warning("Finding Lat/Lon for event {} failed!".format(event.key_name))
    #     return lat_lng

    @classmethod
    def update_event_location(cls, event):
        location_info, score = cls.get_event_location_info(event)
        if score < 0.5:
            return

        if 'lat' in location_info and 'lng' in location_info:
            lat_lng = ndb.GeoPt(location_info['lat'], location_info['lng'])
        else:
            lat_lng = None
        event.normalized_location = Location(
            name=location_info.get('name', None),
            formatted_address=location_info.get('formatted_address', None),
            lat_lng=lat_lng,
            street_number=location_info.get('street_number', None),
            street=location_info.get('street', None),
            city=location_info.get('city', None),
            state_prov=location_info.get('state_prov', None),
            state_prov_short=location_info.get('state_prov_short', None),
            country=location_info.get('country', None),
            country_short=location_info.get('country_short', None),
            postal_code=location_info.get('postal_code', None),
            place_id=location_info.get('place_id', None),
            place_details=location_info.get('place_details', None),
        )

    @classmethod
    def _log_event_location_score(cls, event_key, score):
        text = "Event {} location score: {}".format(event_key, score)
        if score < 0.8:
            logging.warning(text)
        else:
            logging.info(text)

    @classmethod
    def get_event_location_info(cls, event):
        """
        Search for different combinations of venue, venue_address, city,
        state_prov, postalcode, and country in attempt to find the correct
        location associated with the event.
        """
        if not event.location:
            return {}, 0

        # Possible queries for location that will match yield results
        if event.venue:
            possible_queries = [event.venue]
        else:
            possible_queries = []

        lat_lng = None
        if event.venue_address:
            split_address = event.venue_address.split('\n')
            for i in xrange(min(len(split_address), 2)):  # Venue takes up at most 2 lines
                query = ' '.join(split_address[0:i+1])  # From the front
                if query not in possible_queries:
                    possible_queries.append(query)
                query = split_address[i]
                if query not in possible_queries:
                    possible_queries.append(query)

            for i in xrange(len(split_address)):
                query = ' '.join(split_address[i:])  # From the back
                if query not in possible_queries:
                    possible_queries.append(query)

            # Get general lat/lng
            if event.venue_address:
                coarse_results = cls.find_places(' '.join(split_address[1:])).get_result()
                if coarse_results:
                    lat_lng = '{},{}'.format(
                        coarse_results[0]['geometry']['location']['lat'],
                        coarse_results[0]['geometry']['location']['lng'])

        # Try to find place based on possible queries
        best_score = 0
        best_location_info = {}
        nearbysearch_results_candidates = []  # More trustworthy candidates are added first
        for query in possible_queries:
            nearbysearch_results = cls.find_places(query, lat_lng=lat_lng).get_result()
            if nearbysearch_results:
                if len(nearbysearch_results) == 1:
                    location_info = cls.construct_location_info_async(nearbysearch_results[0]).get_result()
                    score = cls.compute_event_location_score(event, query, location_info)
                    if score == 1:
                        # Very likely to be correct if only 1 result and as a perfect score
                        cls._log_event_location_score(event.key.id(), score)
                        return location_info, score
                    elif score > best_score:
                        # Only 1 result but score is imperfect
                        best_score = score
                        best_location_info = location_info
                else:
                    # Save queries with multiple results for later evaluation
                    nearbysearch_results_candidates.append((nearbysearch_results, query))

        # Consider all candidates and find best one
        for nearbysearch_results, query in nearbysearch_results_candidates:
            for nearbysearch_result in nearbysearch_results:
                location_info = cls.construct_location_info_async(nearbysearch_result).get_result()
                score = cls.compute_event_location_score(event, query, location_info)
                if score == 1:
                    cls._log_event_location_score(event.key.id(), score)
                    return location_info, score
                elif score > best_score:
                    best_score = score
                    best_location_info = location_info

        cls._log_event_location_score(event.key.id(), best_score)
        return best_location_info, best_score

    @classmethod
    def compute_event_location_score(cls, event, query, location_info):
        """
        Score for correctness. 1.0 is perfect.
        Not checking for absolute equality in case of existing data errors.
        Check with both long and short names
        """
        max_score = 5.0
        score = 0.0
        if event.country:
            partial = max(
                cls.get_similarity(location_info.get('country', ''), event.country),
                cls.get_similarity(location_info.get('country_short', ''), event.country))
            score += 1 if partial > 0.5 else 0
        if event.state_prov:
            partial = max(
                cls.get_similarity(location_info.get('state_prov', ''), event.state_prov),
                cls.get_similarity(location_info.get('state_prov_short', ''), event.state_prov))
            score += partial if partial > 0.5 else 0
        if event.city:
            partial = cls.get_similarity(location_info.get('city', ''), event.city)
            score += partial if partial > 0.5 else 0
        if event.postalcode:
            partial = cls.get_similarity(location_info.get('postal_code', ''), event.postalcode)
            score += partial if partial > 0.5 else 0

        if location_info.get('name', '') in query and ('point_of_interest' in location_info.get('types', '') or 'premise' in location_info.get('types', '')):
            score += 3  # If name matches, we're probably good
        else:
            partial = cls.get_similarity(location_info.get('name', ''), query)
            score += partial

        if 'point_of_interest' not in location_info.get('types', '') and 'premise' not in location_info.get('types', ''):
            score *= 0.5

        return min(1.0, score / max_score)

    @classmethod
    def update_team_location(cls, team):
        # Try with and without textsearch, pick best
        location_info, score = cls.get_team_location_info(team)
        if score < 0.7:
            logging.warning("Using textsearch for {}".format(team.key.id()))
            location_info2, score2 = cls.get_team_location_info(team, textsearch=True)
            if score2 > score:
                location_info = location_info2
                score = score2

        # Log performance
        text = "Team {} location score: {}".format(team.key.id(), score)
        if score < 0.8:
            logging.warning(text)
        else:
            logging.info(text)

        # Update team
        if 'lat' in location_info and 'lng' in location_info:
            lat_lng = ndb.GeoPt(location_info['lat'], location_info['lng'])
        else:
            lat_lng = None
        team.normalized_location = Location(
            name=location_info.get('name', None),
            formatted_address=location_info.get('formatted_address', None),
            lat_lng=lat_lng,
            street_number=location_info.get('street_number', None),
            street=location_info.get('street', None),
            city=location_info.get('city', None),
            state_prov=location_info.get('state_prov', None),
            state_prov_short=location_info.get('state_prov_short', None),
            country=location_info.get('country', None),
            country_short=location_info.get('country_short', None),
            postal_code=location_info.get('postal_code', None),
            place_id=location_info.get('place_id', None),
            place_details=location_info.get('place_details', None),
        )

    @classmethod
    def get_team_location_info(cls, team, textsearch=False):
        """
        Search for different combinations of team name (which should include
        high school or title sponsor) with city, state_prov, postalcode, and country
        in attempt to find the correct location associated with the team.
        """
        if not team.location:
            return {}, 0

        # Find possible schools/title sponsors
        possible_names = []
        MAX_SPLIT = 3  # Filters out long names that are unlikely
        if team.name:
            # Guessing sponsors/school by splitting name by '/' or '&'
            split1 = re.split('&', team.name)
            split2 = re.split('/', team.name)

            if split1 and \
                    split1[-1].count('&') < MAX_SPLIT and split1[-1].count('/') < MAX_SPLIT:
                possible_names.append(split1[-1])
            if split2 and split2[-1] not in possible_names and \
                     split2[-1].count('&') < MAX_SPLIT and split2[-1].count('/') < MAX_SPLIT:
                possible_names.append(split2[-1])
            if split1 and split1[0] not in possible_names and \
                     split1[0].count('&') < MAX_SPLIT and split1[0].count('/') < MAX_SPLIT:
                possible_names.append(split1[0])
            if split2 and split2[0] not in possible_names and \
                     split2[0].count('&') < MAX_SPLIT and split2[0].count('/') < MAX_SPLIT:
                possible_names.append(split2[0])

        # Geocode for lat/lng
        lat_lng, _ = cls.get_lat_lng_async(team.location).get_result()

        # Try to find place based on possible queries
        best_score = 0
        best_location_info = {}
        nearbysearch_results_candidates = []  # More trustworthy candidates are added first
        for name in possible_names:
            places =  cls.google_maps_placesearch_async(name, lat_lng, textsearch=textsearch).get_result()
            for i, place in enumerate(places[:5]):
                location_info = cls.construct_location_info_async(place).get_result()
                score = cls.compute_team_location_score(team, name, location_info)
                score *= pow(0.7, i)  # discount by ranking
                if score == 1:
                    return location_info, score
                elif score > best_score:
                    best_location_info = location_info
                    best_score = score

        return best_location_info, best_score

    @classmethod
    def compute_team_location_score(cls, team, query_name, location_info):
        """
        Score for correctness. 1.0 is perfect.
        Not checking for absolute equality in case of existing data errors.
        """
        score = pow(cls.get_similarity(query_name, location_info['name']), 1.0/3)
        if not {'school', 'university'}.intersection(set(location_info.get('types', ''))):
            score *= 0.7

        return score

    @classmethod
    @ndb.tasklet
    def construct_location_info_async(cls, nearbysearch_result):
        """
        Gets location info given a nearbysearch result
        """
        location_info = {
            'place_id': nearbysearch_result['place_id'],
            'lat': nearbysearch_result['geometry']['location']['lat'],
            'lng': nearbysearch_result['geometry']['location']['lng'],
            'name': nearbysearch_result['name'],
            'types': nearbysearch_result['types'],
        }
        place_details_result = yield cls.google_maps_place_details_async(nearbysearch_result['place_id'])
        if place_details_result:
            has_city = False
            for component in place_details_result['address_components']:
                if 'street_number' in component['types']:
                    location_info['street_number'] = component['long_name']
                elif 'route' in component['types']:
                    location_info['street'] = component['long_name']
                elif 'locality' in component['types']:
                    location_info['city'] = component['long_name']
                    has_city = True
                elif 'administrative_area_level_1' in component['types']:
                    location_info['state_prov'] = component['long_name']
                    location_info['state_prov_short'] = component['short_name']
                elif 'country' in component['types']:
                    location_info['country'] = component['long_name']
                    location_info['country_short'] = component['short_name']
                elif 'postal_code' in component['types']:
                    location_info['postal_code'] = component['long_name']

            # Special case for when there is no city
            if not has_city and 'state_prov' in location_info:
                location_info['city'] = location_info['state_prov']

            location_info['formatted_address'] = place_details_result['formatted_address']

            # Save everything just in case
            location_info['place_details'] = place_details_result

        raise ndb.Return(location_info)

    @classmethod
    @ndb.tasklet
    def google_maps_placesearch_async(cls, query, lat_lng, textsearch=False):
        """
        https://developers.google.com/places/web-service/search#nearbysearchRequests
        """
        if not cls.GOOGLE_API_KEY:
            GOOGLE_SECRETS = Sitevar.get_by_id("google.secrets")
            if GOOGLE_SECRETS:
                cls.GOOGLE_API_KEY = GOOGLE_SECRETS.contents['api_key']
            else:
                logging.warning("Must have sitevar google.api_key to use Google Maps nearbysearch")
                raise ndb.Return([])

        search_type = 'textsearch' if textsearch else 'nearbysearch'

        results = None
        if query:
            query = query.encode('ascii', 'ignore')
            cache_key = u'google_maps_{}:{}'.format(search_type, query)
            results = memcache.get(cache_key)
            if results is None:
                search_params = {
                    'key': cls.GOOGLE_API_KEY,
                    'location': '{},{}'.format(lat_lng[0], lat_lng[1]),
                    'radius': 25000,
                }
                if textsearch:
                    search_params['query'] = query
                else:
                    search_params['keyword'] = query

                search_url = 'https://maps.googleapis.com/maps/api/place/{}/json?{}'.format(search_type, urllib.urlencode(search_params))
                try:
                    # Make async urlfetch call
                    context = ndb.get_context()
                    search_result = yield context.urlfetch(search_url)

                    # Parse urlfetch result
                    if search_result.status_code == 200:
                        search_dict = json.loads(search_result.content)
                        if search_dict['status'] == 'ZERO_RESULTS':
                            logging.info('No {} results for query: {}, lat_lng: {}'.format(search_type, query, lat_lng))
                        elif search_dict['status'] == 'OK':
                            results = search_dict['results']
                        else:
                            logging.warning(u'{} failed with query: {}, lat_lng: {}'.format(search_type, query, lat_lng))
                            logging.warning(search_dict)
                    else:
                        logging.warning(u'{} failed with query: {}, lat_lng: {}'.format(search_type, query, lat_lng))
                        logging.warning(search_dict)
                except Exception, e:
                    logging.warning(u'urlfetch for {} request failed with query: {}, lat_lng: {}'.format(search_type, query, lat_lng))
                    logging.warning(e)

                memcache.set(cache_key, results if results else [])

        raise ndb.Return(results if results else [])

    @classmethod
    @ndb.tasklet
    def google_maps_place_details_async(cls, place_id):
        """
        https://developers.google.com/places/web-service/details#PlaceDetailsRequests
        """
        if not cls.GOOGLE_API_KEY:
            GOOGLE_SECRETS = Sitevar.get_by_id("google.secrets")
            if GOOGLE_SECRETS:
                cls.GOOGLE_API_KEY = GOOGLE_SECRETS.contents['api_key']
            else:
                logging.warning("Must have sitevar google.api_key to use Google Maps PlaceDetails")
                raise ndb.Return(None)

        result = None
        cache_key = u'google_maps_place_details:{}'.format(place_id)
        result = memcache.get(cache_key)
        if not result:
            place_details_params = {
                'placeid': place_id,
                'key': cls.GOOGLE_API_KEY,
            }
            place_details_url = 'https://maps.googleapis.com/maps/api/place/details/json?%s' % urllib.urlencode(place_details_params)
            try:
                # Make async urlfetch call
                context = ndb.get_context()
                place_details_result = yield context.urlfetch(place_details_url)

                # Parse urlfetch call
                if place_details_result.status_code == 200:
                    place_details_dict = json.loads(place_details_result.content)
                    if place_details_dict['status'] == 'ZERO_RESULTS':
                        logging.info('No place_details result for place_id: {}'.format(place_id))
                    elif place_details_dict['status'] == 'OK':
                        result = place_details_dict['result']
                    else:
                        logging.warning('Placedetails failed with place_id: {}.'.format(place_id))
                        logging.warning(place_details_dict)
                else:
                    logging.warning('Placedetails failed with place_id: {}.'.format(place_id))
            except Exception, e:
                logging.warning('urlfetch for place_details request failed with place_id: {}.'.format(place_id))
                logging.warning(e)

            if tba_config.CONFIG['memcache']:
                memcache.set(cache_key, result)

        raise ndb.Return(result)

    @classmethod
    def get_lat_lng(cls, location):
        """
        DEPRRECATED TODO REMOVE AFTER MIGRATION
        """
        return cls.get_lat_lng_async(location).get_result()

    @classmethod
    @ndb.tasklet
    def get_lat_lng_async(cls, location):
        """
        DEPRRECATED TODO REMOVE AFTER MIGRATION
        """
        cache_key = u'get_lat_lng_{}'.format(location)
        result = memcache.get(cache_key)
        if not result:
            context = ndb.get_context()
            lat_lng = None
            num_results = 0

            if not location:
                raise ndb.Return(lat_lng, num_results)

            location = location.encode('utf-8')

            google_secrets = Sitevar.get_by_id("google.secrets")
            google_api_key = None
            if google_secrets is None:
                logging.warning("Missing sitevar: google.api_key. API calls rate limited by IP and may be over rate limit.")
            else:
                google_api_key = google_secrets.contents['api_key']

            geocode_params = {
                'address': location,
                'sensor': 'false',
            }
            if google_api_key:
                geocode_params['key'] = google_api_key
            geocode_url = 'https://maps.googleapis.com/maps/api/geocode/json?%s' % urllib.urlencode(geocode_params)
            try:
                geocode_result = yield context.urlfetch(geocode_url)
                if geocode_result.status_code == 200:
                    geocode_dict = json.loads(geocode_result.content)
                    if geocode_dict['status'] == 'ZERO_RESULTS':
                        logging.info('No geocode results for location: {}'.format(location))
                    elif geocode_dict['status'] == 'OK':
                        lat_lng = geocode_dict['results'][0]['geometry']['location']['lat'], geocode_dict['results'][0]['geometry']['location']['lng']
                        num_results = len(geocode_dict['results'])
                    else:
                        logging.warning('Geocoding failed!')
                        logging.warning(geocode_dict)
                else:
                    logging.warning('Geocoding failed for location {}.'.format(location))
            except Exception, e:
                logging.warning('urlfetch for geocode request failed for location {}.'.format(location))
                logging.warning(e)

            result = lat_lng, num_results
            memcache.set(cache_key, result)

        raise ndb.Return(result)

    @classmethod
    def get_timezone_id(cls, location, lat_lng=None):
        if lat_lng is None:
            result, _ = cls.get_lat_lng(location)
            if result is None:
                return None
            else:
                lat, lng = result
        else:
            lat, lng = lat_lng

        google_secrets = Sitevar.get_by_id("google.secrets")
        google_api_key = None
        if google_secrets is None:
            logging.warning("Missing sitevar: google.api_key. API calls rate limited by IP and may be over rate limit.")
        else:
            google_api_key = google_secrets.contents['api_key']

        # timezone request
        tz_params = {
            'location': '%s,%s' % (lat, lng),
            'timestamp': 0,  # we only care about timeZoneId, which doesn't depend on timestamp
            'sensor': 'false',
        }
        if google_api_key is not None:
            tz_params['key'] = google_api_key
        tz_url = 'https://maps.googleapis.com/maps/api/timezone/json?%s' % urllib.urlencode(tz_params)
        try:
            tz_result = urlfetch.fetch(tz_url)
        except Exception, e:
            logging.warning('urlfetch for timezone request failed: {}'.format(tz_url))
            logging.info(e)
            return None
        if tz_result.status_code != 200:
            logging.warning('TZ lookup for (lat, lng) failed! ({}, {})'.format(lat, lng))
            return None
        tz_dict = json.loads(tz_result.content)
        if 'timeZoneId' not in tz_dict:
            logging.warning('No timeZoneId for (lat, lng)'.format(lat, lng))
            return None
        return tz_dict['timeZoneId']
